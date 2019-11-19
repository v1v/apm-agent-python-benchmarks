import os
import random
import shutil
import subprocess

import click

import elasticsearch
import pyperf

try:
    from urllib.request import Request, urlopen
    from urllib.parse import urlparse
except ImportError:
    from urllib2 import Request, urlopen
    from urllib.parse import urlparse

BASE_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def get_commit_list(start_commit, end_commit, worktree):
    commit_range = "%s..%s" % (start_commit, end_commit)
    commits = subprocess.check_output(
        ["git", "log", "--pretty=%h", commit_range], cwd=worktree
    ).decode("utf8")
    commit_hashes = commits.split("\n")[:-1]
    commits = []
    for hash in commit_hashes:
        timestamp, _, commit_message = (
            subprocess.check_output(
                ["git", "log", hash, "-1", "--pretty=%aI\t%H\t%B"], cwd=worktree
            )
            .decode("utf8")
            .split("\t", 2)
        )
        commits.append(
            {
                "sha": hash,
                "@timestamp": timestamp,
                "commit_title": commit_message.split("/n")[0],
                "commit_message": commit_message,
            }
        )
    commits.reverse()
    return commits


def run_benchmark(commit_info, worktree):
    if commit_info:
        subprocess.check_output(["git", "checkout", commit_info["sha"]], cwd=worktree)
    env = dict(**os.environ)
    env["PYTHONPATH"] = worktree
    env["COMMIT_TIMESTAMP"] = commit_info["@timestamp"]
    env["COMMIT_SHA"] = commit_info["sha"]
    env["COMMIT_MESSAGE"] = commit_info["commit_title"]
    output_files = []
    for bench_type, flag in (("time", None), ("tracemalloc", "--tracemalloc")):
        output_file = "result.%s.%s.json" % (bench_type, commit_info["sha"])
        test_cmd = [
            "python",
            "run_bench.py",
            "-o",
            output_file,
            "--inherit-environ",
            "COMMIT_TIMESTAMP,COMMIT_SHA,COMMIT_MESSAGE,PYTHONPATH",
        ]
        if flag:
            test_cmd.append(flag)
        print(
            subprocess.check_output(
                test_cmd, stderr=subprocess.STDOUT, env=env
            ).decode()
        )
        output_files.append(output_file)
    return output_files


def upload_benchmark(es_url, es_user, es_password, files, commit_info):
    if "@" not in es_url and es_user:
        parts = urlparse(es_url)
        es_url = "%s://%s:%s@%s%s" % (
            parts.scheme,
            es_user,
            es_password,
            parts.netloc,
            parts.path,
        )
    es = elasticsearch.Elasticsearch([es_url])
    result = []
    for file in files:
        suite = pyperf.BenchmarkSuite.load(file)
        for bench in suite:
            ncalibration_runs = sum(run._is_calibration() for run in bench._runs)
            nrun = bench.get_nrun()
            loops = bench.get_loops()
            inner_loops = bench.get_inner_loops()
            total_loops = loops * inner_loops
            meta = bench.get_metadata()
            meta["start_date"] = bench.get_dates()[0].isoformat(" ")
            if meta["unit"] == "second":
                meta["unit"] = "milliseconds"
                result_factor = 1000
            else:
                result_factor = 1
            full_name = meta.pop("name")
            class_name = full_name.rsplit(".", 1)[0]
            short_name = class_name.rsplit(".", 1)[0]
            output = {
                "_index": "benchmark-python",
                "@timestamp": meta.pop("timestamp"),
                "benchmark_class": class_name,
                "benchmark_short_name": short_name,
                "benchmark": full_name,
                "meta": meta,
                "runs": {
                    "calibration": ncalibration_runs,
                    "with_values": nrun - ncalibration_runs,
                    "total": nrun,
                },
                "warmups_per_run": bench._get_nwarmup(),
                "values_per_run": bench._get_nvalue_per_run(),
                "median": bench.median() * result_factor,
                "median_abs_dev": bench.median_abs_dev() * result_factor,
                "mean": bench.mean() * result_factor,
                "mean_std_dev": bench.stdev() * result_factor,
                "percentiles": {},
            }
            for p in (0, 5, 25, 50, 75, 95, 99, 100):
                output["percentiles"]["%.1f" % p] = bench.percentile(p) * result_factor
            result.append(output)
    for b in result:
        es.index(body=b, index=b.pop("_index"))
    es.update(
        index="benchmark-py-commits",
        id=commit_info["sha"],
        body={
            "doc": {
                "@timestamp": commit_info["@timestamp"],
                "commit_title": commit_info["commit_title"].split("\n")[0],
                "commit_message": commit_info["commit_message"],
            },
            "doc_as_upsert": True,
        },
    )


@click.command()
@click.option(
    "--worktree",
    required=True,
    type=click.Path(),
    help="worktree of elastic-apm to run benchmarks in",
)
@click.option(
    "--start-commit",
    default=None,
    help="first commit to benchmark. If left empty, current worktree state will be benchmarked",
)
@click.option(
    "--end-commit",
    default=None,
    help="last commit to benchmark. If left empty, only start-commit will be benchmarked",
)
@click.option("--clone-url", default=None, help="Git URL to clone")
@click.option("--es-url", default=None, help="Elasticsearch URL")
@click.option("--es-user", default=None, help="Elasticsearch User")
@click.option(
    "--es-password", default=None, help="Elasticsearch Password", envvar="ES_PASSWORD"
)
@click.option(
    "--delete-output-files/--no-delete-output-files",
    default=False,
    help="Delete benchmark files",
)
@click.option(
    "--delete-repo/--no-delete-repo", default=False, help="Delete repo after run"
)
@click.option(
    "--randomize/--no-randomize", default=True, help="Randomize order of commits"
)
def run(
    worktree,
    start_commit,
    end_commit,
    clone_url,
    es_url,
    es_user,
    es_password,
    delete_output_files,
    delete_repo,
    randomize,
):
    if clone_url:
        if not os.path.exists(worktree):
            subprocess.check_output(["git", "clone", clone_url, worktree])
    subprocess.check_output(["git", "fetch"], cwd=worktree)
    if start_commit:
        if end_commit:
            commits = get_commit_list(start_commit, end_commit, worktree)
        else:
            commits = [start_commit]
    else:
        commits = [None]
    json_files = []
    failed = []
    if randomize:
        random.shuffle(commits)
    for i, commit in enumerate(commits):
        if len(commits) > 1:
            print(
                "Running bench for commit {} ({} of {})".format(
                    commit["sha"][:8], i + 1, len(commits)
                )
            )
        try:
            files = run_benchmark(commit, worktree)
            if es_url:
                print("Uploading bench for commit {}".format(commit["sha"][:8]))
                upload_benchmark(es_url, es_user, es_password, files, commit)
            json_files.extend(files)
        except Exception:
            failed.append(commit["sha"])
    if delete_repo:
        shutil.rmtree(worktree)
    if delete_output_files:
        for file in json_files:
            os.unlink(file)
    if failed:
        print("Failed commits: \n")
        for commit in failed:
            print(commit)
        print()


if __name__ == "__main__":
    run()
