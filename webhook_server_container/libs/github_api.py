from __future__ import annotations

import contextlib
import json
import logging
import os
import random
import re
import shutil
import time
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable, Generator
from uuid import uuid4

import requests
import shortuuid
import yaml
from github import GithubException
from github.Branch import Branch
from github.CheckRun import CheckRun
from github.Commit import Commit
from github.GithubException import UnknownObjectException
from github.PullRequest import PullRequest
from github.Repository import Repository
from starlette.datastructures import Headers
from stringcolor import cs
from timeout_sampler import TimeoutExpiredError, TimeoutSampler

from webhook_server_container.libs.config import Config
from webhook_server_container.libs.jira_api import JiraApi
from webhook_server_container.utils.constants import (
    ADD_STR,
    APPROVED_BY_LABEL_PREFIX,
    BRANCH_LABEL_PREFIX,
    BUILD_AND_PUSH_CONTAINER_STR,
    BUILD_CONTAINER_STR,
    CAN_BE_MERGED_STR,
    CHANGED_REQUESTED_BY_LABEL_PREFIX,
    CHERRY_PICK_LABEL_PREFIX,
    CHERRY_PICKED_LABEL_PREFIX,
    COMMAND_ASSIGN_REVIEWER_STR,
    COMMAND_ASSIGN_REVIEWERS_STR,
    COMMAND_CHECK_CAN_MERGE_STR,
    COMMAND_CHERRY_PICK_STR,
    COMMAND_RETEST_STR,
    COMMENTED_BY_LABEL_PREFIX,
    CONVENTIONAL_TITLE_STR,
    DELETE_STR,
    DYNAMIC_LABELS_DICT,
    FAILURE_STR,
    HAS_CONFLICTS_LABEL_STR,
    HOLD_LABEL_STR,
    IN_PROGRESS_STR,
    JIRA_STR,
    LGTM_BY_LABEL_PREFIX,
    LGTM_STR,
    NEEDS_REBASE_LABEL_STR,
    OTHER_MAIN_BRANCH,
    PRE_COMMIT_STR,
    PYTHON_MODULE_INSTALL_STR,
    QUEUED_STR,
    REACTIONS,
    SIZE_LABEL_PREFIX,
    STATIC_LABELS_DICT,
    SUCCESS_STR,
    TOX_STR,
    USER_LABELS_DICT,
    VERIFIED_LABEL_STR,
    WIP_STR,
)
from webhook_server_container.utils.github_repository_settings import (
    get_repository_github_app_api,
)
from webhook_server_container.utils.helpers import (
    extract_key_from_dict,
    get_api_with_highest_rate_limit,
    get_apis_and_tokes_from_config,
    get_github_repo_api,
    get_value_from_dicts,
    run_command,
)


class NoPullRequestError(Exception):
    pass


class RepositoryNotFoundError(Exception):
    pass


class ProcessGithubWehookError(Exception):
    def __init__(self, err: dict[str, str]):
        self.err = err

    def __str__(self) -> str:
        return f"{self.err}"


class RepositoryNotInitializedError(Exception):
    pass


class ProcessGithubWehook:
    def __init__(self, hook_data: dict[Any, Any], headers: Headers, logger: logging.Logger) -> None:
        self.logger = logger
        self.logger.name = "ProcessGithubWehook"
        self.hook_data = hook_data
        self.headers = headers
        self.repository: Repository | None = None
        self.repository_name: str = hook_data["repository"]["name"]
        self.repository_full_name: str = self.hook_data["repository"]["full_name"]
        self.parent_committer: str = ""
        self.jira_track_pr: bool = False
        self.issue_title: str = ""
        self.all_required_status_checks: list[str] = []
        self.x_github_delivery: str = self.headers.get("X-GitHub-Delivery", "")
        self.github_event: str = self.headers["X-GitHub-Event"]
        self.owners_content: dict[str, Any] = {}

        self.config = Config()
        self.log_prefix = self.prepare_log_prefix()
        self.github_app_api = get_repository_github_app_api(
            config_=self.config, repository_name=self.repository_full_name
        )

        if not self.github_app_api:
            self.logger.error(
                (
                    f"{self.log_prefix} not found by manage-repositories-app, "
                    "make sure the app installed (https://github.com/apps/manage-repositories-app)"
                ),
            )
            return

        self._repo_data_from_config()

        self.github_api, self.token = get_api_with_highest_rate_limit(
            config=self.config, repository_name=self.repository_name
        )

        if self.github_api and self.token:
            self.repository = get_github_repo_api(github_api=self.github_api, repository=self.repository_full_name)

        else:
            self.logger.error(f"{self.log_prefix} Failed to get GitHub API and token.")
            return

        with contextlib.suppress(NoPullRequestError):
            self.pull_request = self._get_pull_request()
            self.pull_request_branch = self.pull_request.base.ref

        self.repository_by_github_app = get_github_repo_api(
            github_api=self.github_app_api, repository=self.repository_full_name
        )

        if not (self.repository or self.repository_by_github_app):
            self.logger.error(f"{self.log_prefix} Failed to get repository.")
            return

        self.clone_repo_dir: str = os.path.join("/tmp", f"{self.repository.name}")
        self.add_api_users_to_auto_verified_and_merged_users()

        self.supported_user_labels_str: str = "".join([f" * {label}\n" for label in USER_LABELS_DICT.keys()])
        self.current_pull_request_supported_retest = self._current_pull_request_supported_retest
        self.welcome_msg: str = f"""
Report bugs in [Issues](https://github.com/myakove/github-webhook-server/issues)

The following are automatically added:
 * Add reviewers from OWNER file (in the root of the repository) under reviewers section.
 * Set PR size label.
 * New issue is created for the PR. (Closed when PR is merged/closed)
 * Run [pre-commit](https://pre-commit.ci/) if `.pre-commit-config.yaml` exists in the repo.

Available user actions:
 * To mark PR as WIP comment `/wip` to the PR, To remove it from the PR comment `/wip cancel` to the PR.
 * To block merging of PR comment `/hold`, To un-block merging of PR comment `/hold cancel`.
 * To mark PR as verified comment `/verified` to the PR, to un-verify comment `/verified cancel` to the PR.
        verified label removed on each new commit push.
 * To cherry pick a merged PR comment `/cherry-pick <target branch to cherry-pick to>` in the PR.
    * Multiple target branches can be cherry-picked, separated by spaces. (`/cherry-pick branch1 branch2`)
    * Cherry-pick will be started when PR is merged
 * To build and push container image command `/build-and-push-container` in the PR (tag will be the PR number).
    * You can add extra args to the Podman build command
        * Example: `/build-and-push-container --build-arg OPENSHIFT_PYTHON_WRAPPER_COMMIT=<commit_hash>`
 * To add a label by comment use `/<label name>`, to remove, use `/<label name> cancel`
 * To assign reviewers based on OWNERS file use `/assign-reviewers`
 * To check if PR can be merged use `/check-can-merge`
 * to assign reviewer to PR use `/assign-reviewer @<reviewer>`

<details>
<summary>Supported /retest check runs</summary>

{self.prepare_retest_wellcome_msg}
</details>

<details>
<summary>Supported labels</summary>

{self.supported_user_labels_str}
</details>
    """

    def process(self) -> None:
        if self.github_event == "ping":
            return

        event_log: str = f"Event type: {self.github_event}. event ID: {self.x_github_delivery}"

        try:
            if not self.pull_request:
                self.pull_request = self._get_pull_request()

            self.log_prefix = self.prepare_log_prefix(pull_request=self.pull_request)
            self.logger.debug(f"{self.log_prefix} {event_log}")
            self.last_commit = self._get_last_commit()
            self.parent_committer = self.pull_request.user.login
            self.last_committer = getattr(self.last_commit.committer, "login", self.parent_committer)
            self.changed_files = self.list_changed_files()
            self.pull_request_branch = self.pull_request.base.ref
            self.all_approvers_and_reviewers = self.get_all_approvers_and_reviewers()
            self.all_approvers = self.get_all_approvers()
            self.all_reviewers = self.get_all_reviewers()

            if self.jira_enabled_repository:
                self.set_jira_in_pull_request()

            if self.github_event == "issue_comment":
                self.process_comment_webhook_data()

            elif self.github_event == "pull_request":
                self.process_pull_request_webhook_data()

            elif self.github_event == "pull_request_review":
                self.process_pull_request_review_webhook_data()

            elif self.github_event == "check_run":
                self.process_pull_request_check_run_webhook_data()

        except NoPullRequestError:
            self.logger.debug(f"{self.log_prefix} {event_log}. [No pull request found in hook data]")

            if self.github_event == "push":
                self.process_push_webhook_data()

            elif self.github_event == "check_run":
                self.process_pull_request_check_run_webhook_data()

    @property
    def prepare_retest_wellcome_msg(self) -> str:
        retest_msg: str = ""
        if self.tox:
            retest_msg += f" * `/retest {TOX_STR}`: Retest tox\n"

        if self.build_and_push_container:
            retest_msg += f" * `/retest {BUILD_CONTAINER_STR}`: Retest build-container\n"

        if self.pypi:
            retest_msg += f" * `/retest {PYTHON_MODULE_INSTALL_STR}`: Retest python-module-install\n"

        if self.pre_commit:
            retest_msg += f" * `/retest {PRE_COMMIT_STR}`: Retest pre-commit\n"

        if retest_msg:
            retest_msg += " * `/retest all`: Retest all\n"

        return " * This repository does not support retest actions" if not retest_msg else retest_msg

    def add_api_users_to_auto_verified_and_merged_users(self) -> None:
        apis_and_tokens = get_apis_and_tokes_from_config(config=self.config, repository_name=self.repository_name)
        self.auto_verified_and_merged_users.extend([_api[0].get_user().login for _api in apis_and_tokens])

    def _get_reposiroty_color_for_log_prefix(self) -> str:
        def _get_random_color(_colors: list[str], _json: dict[str, str]) -> str:
            color = random.choice(_colors)
            _json[self.repository_name] = color

            if _selected := cs(self.repository_name, color).render():
                return _selected

            return self.repository_name

        _all_colors: list[str] = []
        color_json: dict[str, str]
        _colors_to_exclude = ("blue", "white", "black", "grey")
        color_file: str = os.path.join(self.config.data_dir, "log-colors.json")

        for _color_name in cs.colors.values():
            _cname = _color_name["name"]
            if _cname.lower() in _colors_to_exclude:
                continue

            _all_colors.append(_cname)

        try:
            with open(color_file) as fd:
                color_json = json.load(fd)

        except Exception:
            color_json = {}

        if color := color_json.get(self.repository_name, ""):
            _cs_object = cs(self.repository_name, color)
            if cs.find_color(_cs_object):
                _str_color = _cs_object.render()

            else:
                _str_color = _get_random_color(_colors=_all_colors, _json=color_json)

        else:
            _str_color = _get_random_color(_colors=_all_colors, _json=color_json)

        with open(color_file, "w") as fd:
            json.dump(color_json, fd)

        if _str_color:
            _str_color = _str_color.replace("\x1b", "\033")
            return _str_color

        return self.repository_name

    def prepare_log_prefix(self, pull_request: PullRequest | None = None) -> str:
        _repository_color = self._get_reposiroty_color_for_log_prefix()
        return (
            f"{_repository_color}[{self.github_event}][{self.x_github_delivery}][PR {pull_request.number}]:"
            if pull_request
            else f"{_repository_color}[{self.github_event}][{self.x_github_delivery}]:"
        )

    def process_pull_request_check_run_webhook_data(self) -> None:
        if not self.repository:
            raise RepositoryNotInitializedError(f"{self.log_prefix} Repository is not initialized")

        _check_run: dict[str, Any] = self.hook_data["check_run"]
        check_run_name: str = _check_run["name"]

        if self.hook_data.get("action", "") != "completed":
            self.logger.debug(
                f"{self.log_prefix} check run {check_run_name} action is {self.hook_data.get('action', 'N/A')} and not completed, skipping"
            )
            return

        check_run_status: str = _check_run["status"]
        check_run_conclusion: str = _check_run["conclusion"]
        check_run_head_sha: str = _check_run["head_sha"]
        self.logger.debug(
            f"{self.log_prefix} processing check_run - Name: {check_run_name} Status: {check_run_status} Conclusion: {check_run_conclusion}"
        )

        if check_run_name == CAN_BE_MERGED_STR:
            self.logger.debug(f"{self.log_prefix} check run is {CAN_BE_MERGED_STR}, skipping")
            return

        if getattr(self, "pull_request", None):
            return self.check_if_can_be_merged()

        self.logger.debug(
            f"{self.log_prefix} No pull request found in hook data, searching for pull request by head sha"
        )
        for _pull_request in self.repository.get_pulls(state="open"):
            if _pull_request.head.sha == check_run_head_sha:
                self.pull_request = _pull_request
                self.last_commit = self._get_last_commit()
                return self.check_if_can_be_merged()

        self.logger.error(f"{self.log_prefix} No pull request found")

    def _repo_data_from_config(self) -> None:
        local_repo_dict: dict[str, Any] = {}
        config_data = self.config.data  # Global repositories configuration
        repo_data = self.config.repository_data(
            repository_name=self.repository_name
        )  # Specific repository configuration

        if self.github_app_api and hasattr(self, "pull_request"):
            repository = self.github_app_api.get_repo(self.repository_full_name)
            try:
                self.logger.debug(f"{self.log_prefix} getting repository config from {self.pull_request_branch} branch")
                content = repository.get_contents(".gethub-webhok_server.config", ref=self.pull_request_branch)
                if isinstance(content, list):
                    content = content[0]

                local_repo_data: str = content.decoded_content.decode()
                local_repo_dict = yaml.safe_load(local_repo_data)
            except Exception:
                pass

        if not repo_data or local_repo_dict:
            raise RepositoryNotFoundError(f"Repository {self.repository_name} not found in config file")

        # self.github_app_id: str = get_value_from_dicts(
        #     primary_dict=repo_data, secondary_dict=config_data, key="github-app-id"
        # )
        dicts = {
            "primary_dict": local_repo_dict,
            "secondary_dict": repo_data,
            "third_dict": config_data,
        }
        self.pypi: dict[str, str] = get_value_from_dicts(**dicts, key="pypi")
        self.verified_job: bool = get_value_from_dicts(
            **dicts,
            key="verified-job",
            return_on_none=True,
        )
        self.tox: dict[str, str] = get_value_from_dicts(**dicts, key="tox")
        self.tox_python_version: str = get_value_from_dicts(
            **dicts,
            key="tox-python-version",
            return_on_none=None,
        )
        self.slack_webhook_url: str = get_value_from_dicts(**dicts, key="slack_webhook_url")
        self.build_and_push_container: dict[str, Any] = get_value_from_dicts(
            **dicts, key="container", return_on_none={}
        )
        if self.build_and_push_container:
            self.container_repository_username: str = self.build_and_push_container["username"]
            self.container_repository_password: str = self.build_and_push_container["password"]
            self.container_repository: str = self.build_and_push_container["repository"]
            self.dockerfile: str = self.build_and_push_container.get("dockerfile", "Dockerfile")
            self.container_tag: str = self.build_and_push_container.get("tag", "latest")
            self.container_build_args: str = self.build_and_push_container.get("build-args", "")
            self.container_command_args: str = self.build_and_push_container.get("args", "")
            self.container_release: bool = self.build_and_push_container.get("release", False)

        self.pre_commit: bool = get_value_from_dicts(
            **dicts,
            key="pre-commit",
            return_on_none=False,
        )

        self.jira_enabled_repository: bool = False
        self.jira_tracking: bool = get_value_from_dicts(**dicts, key="jira-tracking")
        self.jira: dict[str, Any] = get_value_from_dicts(primary_dict=repo_data, secondary_dict=config_data, key="jira")
        if self.jira_tracking and self.jira:
            self.jira_server: str = self.jira["server"]
            self.jira_project: str = self.jira["project"]
            self.jira_token: str = self.jira["token"]
            self.jira_epic: str = self.jira.get("epic", "")
            self.jira_user_mapping: dict[str, str] = self.jira.get("user-mapping", {})
            self.jira_enabled_repository = all([self.jira_server, self.jira_project, self.jira_token])
            if not self.jira_enabled_repository:
                self.logger.error(
                    f"{self.log_prefix} Jira configuration is not valid. Server: {self.jira_server}, "
                    f"Project: {self.jira_project}, Token: {self.jira_token}"
                )

        self.auto_verified_and_merged_users: list[str] = get_value_from_dicts(
            **dicts,
            key="auto-verified-and-merged-users",
            return_on_none=[],
        )
        self.can_be_merged_required_labels = get_value_from_dicts(
            **dicts,
            key="can-be-merged-required-labels",
            return_on_none=[],
        )
        self.conventional_title: str = get_value_from_dicts(**dicts, key="conventional-title")

    def _get_pull_request(self, number: int | None = None) -> PullRequest:
        if not self.repository:
            raise RepositoryNotInitializedError(f"{self.log_prefix} Repository is not initialized")

        if number:
            return self.repository.get_pull(number)

        for _number in extract_key_from_dict(key="number", _dict=self.hook_data):
            try:
                return self.repository.get_pull(_number)
            except GithubException:
                continue

        commit: dict[str, Any] = self.hook_data.get("commit", {})
        if commit:
            commit_obj = self.repository.get_commit(commit["sha"])
            with contextlib.suppress(Exception):
                return commit_obj.get_pulls()[0]

        raise NoPullRequestError(f"{self.log_prefix} No issue or pull_request found in hook data")

    def _get_last_commit(self) -> Commit:
        return list(self.pull_request.get_commits())[-1]

    def label_exists_in_pull_request(self, label: str) -> bool:
        return any(lb for lb in self.pull_request_labels_names() if lb == label)

    def pull_request_labels_names(self) -> list[str]:
        return [lb.name for lb in self.pull_request.labels] if self.pull_request else []

    def skip_if_pull_request_already_merged(self) -> bool:
        if self.pull_request and self.pull_request.is_merged():
            self.logger.info(f"{self.log_prefix}: PR is merged, not processing")
            return True

        return False

    def _remove_label(self, label: str) -> bool:
        try:
            if self.label_exists_in_pull_request(label=label):
                self.logger.info(f"{self.log_prefix} Removing label {label}")
                self.pull_request.remove_from_labels(label)
                return self.wait_for_label(label=label, exists=False)
        except Exception as exp:
            self.logger.debug(f"{self.log_prefix} Failed to remove {label} label. Exception: {exp}")
            return False

        self.logger.debug(f"{self.log_prefix} Label {label} not found and cannot be removed")
        return False

    def _add_label(self, label: str) -> None:
        if not self.repository:
            raise RepositoryNotInitializedError(f"{self.log_prefix} Repository is not initialized")

        label = label.strip()
        if len(label) > 49:
            self.logger.debug(f"{label} is to long, not adding.")
            return

        if self.label_exists_in_pull_request(label=label):
            self.logger.debug(f"{self.log_prefix} Label {label} already assign")
            return

        if label in STATIC_LABELS_DICT:
            self.logger.info(f"{self.log_prefix} Adding pull request label {label}")
            self.pull_request.add_to_labels(label)
            return

        _color = [DYNAMIC_LABELS_DICT[_label] for _label in DYNAMIC_LABELS_DICT if _label in label]
        self.logger.debug(f"{self.log_prefix} Label {label} was {'found' if _color else 'not found'} in labels dict")
        color = _color[0] if _color else "D4C5F9"
        _with_color_msg = f"repository label {label} with color {color}"

        try:
            _repo_label = self.repository.get_label(label)
            _repo_label.edit(name=_repo_label.name, color=color)
            self.logger.debug(f"{self.log_prefix} Edit {_with_color_msg}")

        except UnknownObjectException:
            self.logger.debug(f"{self.log_prefix} Add {_with_color_msg}")
            self.repository.create_label(name=label, color=color)

        self.logger.info(f"{self.log_prefix} Adding pull request label {label}")
        self.pull_request.add_to_labels(label)
        self.wait_for_label(label=label, exists=True)

    def wait_for_label(self, label: str, exists: bool) -> bool:
        try:
            for sample in TimeoutSampler(
                wait_timeout=30,
                sleep=5,
                func=self.label_exists_in_pull_request,
                label=label,
            ):
                if sample == exists:
                    return True

        except TimeoutExpiredError:
            self.logger.debug(f"{self.log_prefix} Label {label} {'not found' if exists else 'found'}")

        return False

    def _generate_issue_title(self) -> str:
        return f"{self.pull_request.title} - {self.pull_request.number}"

    def _generate_issue_body(self) -> str:
        return f"[Auto generated]\nNumber: [#{self.pull_request.number}]"

    def is_branch_exists(self, branch: str) -> Branch | None:
        if not self.repository:
            raise RepositoryNotInitializedError(f"{self.log_prefix} Repository is not initialized")

        return self.repository.get_branch(branch)

    def upload_to_pypi(self, tag_name: str) -> None:
        def _error(_out: str, _err: str) -> None:
            if not self.repository:
                raise RepositoryNotInitializedError(f"{self.log_prefix} Repository is not initialized")

            err: str = "Publish to pypi failed"
            self.logger.error(f"{self.log_prefix} {err} - {_err}, {_out}")
            self.repository.create_issue(
                title=_err,
                assignee=self.root_approvers[0] if self.root_approvers else "",
                body=f"""
stdout: `{_out}`
stderr: `{_err}`
""",
            )

        clone_repo_dir = f"{self.clone_repo_dir}-{uuid4()}"
        uv_cmd_dir = f"--directory {clone_repo_dir}"
        out: str = ""
        self.logger.info(f"{self.log_prefix} Start uploading to pypi")
        _dist_dir: str = f"{clone_repo_dir}/pypi-dist"

        with self._prepare_cloned_repo_dir(checkout=tag_name, clone_repo_dir=clone_repo_dir):
            rc, out, err = run_command(
                command=f"uv {uv_cmd_dir} build --sdist --out-dir {_dist_dir}", log_prefix=self.log_prefix
            )
            if not rc:
                return _error(_out=out, _err=err)

            rc, tar_gz_file, err = run_command(command=f"ls {_dist_dir}", log_prefix=self.log_prefix)
            if not rc:
                return _error(_out=out, _err=err)

            tar_gz_file = tar_gz_file.strip()

            commands: list[str] = [
                f"uvx {uv_cmd_dir} twine check {_dist_dir}/{tar_gz_file}",
                f"uvx {uv_cmd_dir} twine upload --username __token__ --password {self.pypi['token']} {_dist_dir}/{tar_gz_file} --skip-existing",
            ]
            for cmd in commands:
                rc, out, err = run_command(command=cmd, log_prefix=self.log_prefix)
                if not rc:
                    return _error(_out=out, _err=err)

            self.logger.info(f"{self.log_prefix} Publish to pypi finished")
            if self.slack_webhook_url:
                message: str = f"""
```
{self.repository_name} Version {tag_name} published to PYPI.
```
"""
                self.send_slack_message(message=message, webhook_url=self.slack_webhook_url)

    @property
    def root_reviewers(self) -> list[str]:
        _reviewers = self.all_approvers_and_reviewers.get(".", {}).get("reviewers", [])
        self.logger.debug(f"{self.log_prefix} ROOT Reviewers: {_reviewers}")
        return _reviewers

    @property
    def root_approvers(self) -> list[str]:
        _approvers = self.all_approvers_and_reviewers.get(".", {}).get("approvers", [])
        self.logger.debug(f"{self.log_prefix} ROOT Approvers: {_approvers}")
        return _approvers

    def list_changed_files(self) -> list[str]:
        return [_file.filename for _file in self.pull_request.get_files()]

    def assign_reviewers(self) -> None:
        self.logger.info(f"{self.log_prefix} Assign reviewers")

        _to_add: list[str] = list(set(self.all_reviewers))
        self.logger.debug(f"{self.log_prefix} Reviewers to add: {', '.join(_to_add)}")

        for reviewer in _to_add:
            if reviewer != self.pull_request.user.login:
                self.logger.debug(f"{self.log_prefix} Adding reviewer {reviewer}")
                try:
                    self.pull_request.create_review_request([reviewer])
                except GithubException as ex:
                    self.logger.debug(f"{self.log_prefix} Failed to add reviewer {reviewer}. {ex}")
                    self.pull_request.create_issue_comment(f"{reviewer} can not be added as reviewer. {ex}")

    def get_size(self) -> str:
        """Calculates size label based on additions and deletions."""

        size = self.pull_request.additions + self.pull_request.deletions

        # Define label thresholds in a more readable way
        threshold_sizes = [20, 50, 100, 300, 500]
        prefixes = ["XS", "S", "M", "L", "XL"]

        for i, size_threshold in enumerate(threshold_sizes):
            if size < size_threshold:
                _label = prefixes[i]
                return f"{SIZE_LABEL_PREFIX}{_label}"

        return f"{SIZE_LABEL_PREFIX}XXL"

    def add_size_label(self) -> None:
        """Add a size label to the pull request based on its additions and deletions."""
        size_label = self.get_size()
        if not size_label:
            self.logger.debug(f"{self.log_prefix} Size label not found")
            return

        if size_label in self.pull_request_labels_names():
            return

        exists_size_label = [label for label in self.pull_request_labels_names() if label.startswith(SIZE_LABEL_PREFIX)]

        if exists_size_label:
            self._remove_label(label=exists_size_label[0])

        self._add_label(label=size_label)

    def label_by_user_comment(
        self,
        user_requested_label: str,
        remove: bool,
        reviewed_user: str,
    ) -> None:
        self.logger.debug(
            f"{self.log_prefix} {DELETE_STR if remove else ADD_STR} "
            f"label requested by user {reviewed_user}: {user_requested_label}"
        )

        if user_requested_label == LGTM_STR:
            self.manage_reviewed_by_label(
                review_state=LGTM_STR,
                action=DELETE_STR if remove else ADD_STR,
                reviewed_user=reviewed_user,
            )

        else:
            label_func = self._remove_label if remove else self._add_label
            label_func(label=user_requested_label)

    def set_verify_check_queued(self) -> None:
        return self.set_check_run_status(check_run=VERIFIED_LABEL_STR, status=QUEUED_STR)

    def set_verify_check_success(self) -> None:
        return self.set_check_run_status(check_run=VERIFIED_LABEL_STR, conclusion=SUCCESS_STR)

    def set_run_tox_check_queued(self) -> None:
        if not self.tox:
            return

        return self.set_check_run_status(check_run=TOX_STR, status=QUEUED_STR)

    def set_run_tox_check_in_progress(self) -> None:
        return self.set_check_run_status(check_run=TOX_STR, status=IN_PROGRESS_STR)

    def set_run_tox_check_failure(self, output: dict[str, Any]) -> None:
        return self.set_check_run_status(check_run=TOX_STR, conclusion=FAILURE_STR, output=output)

    def set_run_tox_check_success(self, output: dict[str, Any]) -> None:
        return self.set_check_run_status(check_run=TOX_STR, conclusion=SUCCESS_STR, output=output)

    def set_run_pre_commit_check_queued(self) -> None:
        if not self.pre_commit:
            return

        return self.set_check_run_status(check_run=PRE_COMMIT_STR, status=QUEUED_STR)

    def set_run_pre_commit_check_in_progress(self) -> None:
        return self.set_check_run_status(check_run=PRE_COMMIT_STR, status=IN_PROGRESS_STR)

    def set_run_pre_commit_check_failure(self, output: dict[str, Any] | None = None) -> None:
        return self.set_check_run_status(check_run=PRE_COMMIT_STR, conclusion=FAILURE_STR, output=output)

    def set_run_pre_commit_check_success(self, output: dict[str, Any] | None = None) -> None:
        return self.set_check_run_status(check_run=PRE_COMMIT_STR, conclusion=SUCCESS_STR, output=output)

    def set_merge_check_queued(self, output: dict[str, Any] | None = None) -> None:
        return self.set_check_run_status(check_run=CAN_BE_MERGED_STR, status=QUEUED_STR, output=output)

    def set_merge_check_in_progress(self) -> None:
        return self.set_check_run_status(check_run=CAN_BE_MERGED_STR, status=IN_PROGRESS_STR)

    def set_merge_check_success(self) -> None:
        return self.set_check_run_status(check_run=CAN_BE_MERGED_STR, conclusion=SUCCESS_STR)

    def set_merge_check_failure(self, output: dict[str, Any]) -> None:
        return self.set_check_run_status(check_run=CAN_BE_MERGED_STR, conclusion=FAILURE_STR, output=output)

    def set_container_build_queued(self) -> None:
        if not self.build_and_push_container:
            return

        return self.set_check_run_status(check_run=BUILD_CONTAINER_STR, status=QUEUED_STR)

    def set_container_build_in_progress(self) -> None:
        return self.set_check_run_status(check_run=BUILD_CONTAINER_STR, status=IN_PROGRESS_STR)

    def set_container_build_success(self, output: dict[str, Any]) -> None:
        return self.set_check_run_status(check_run=BUILD_CONTAINER_STR, conclusion=SUCCESS_STR, output=output)

    def set_container_build_failure(self, output: dict[str, Any]) -> None:
        return self.set_check_run_status(check_run=BUILD_CONTAINER_STR, conclusion=FAILURE_STR, output=output)

    def set_python_module_install_queued(self) -> None:
        if not self.pypi:
            return

        return self.set_check_run_status(check_run=PYTHON_MODULE_INSTALL_STR, status=QUEUED_STR)

    def set_python_module_install_in_progress(self) -> None:
        return self.set_check_run_status(check_run=PYTHON_MODULE_INSTALL_STR, status=IN_PROGRESS_STR)

    def set_python_module_install_success(self, output: dict[str, Any]) -> None:
        return self.set_check_run_status(check_run=PYTHON_MODULE_INSTALL_STR, conclusion=SUCCESS_STR, output=output)

    def set_python_module_install_failure(self, output: dict[str, Any]) -> None:
        return self.set_check_run_status(check_run=PYTHON_MODULE_INSTALL_STR, conclusion=FAILURE_STR, output=output)

    def set_conventional_title_queued(self) -> None:
        return self.set_check_run_status(check_run=CONVENTIONAL_TITLE_STR, status=QUEUED_STR)

    def set_conventional_title_in_progress(self) -> None:
        return self.set_check_run_status(check_run=CONVENTIONAL_TITLE_STR, status=IN_PROGRESS_STR)

    def set_conventional_title_success(self, output: dict[str, Any]) -> None:
        return self.set_check_run_status(check_run=CONVENTIONAL_TITLE_STR, conclusion=SUCCESS_STR, output=output)

    def set_conventional_title_failure(self, output: dict[str, Any]) -> None:
        return self.set_check_run_status(check_run=CONVENTIONAL_TITLE_STR, conclusion=FAILURE_STR, output=output)

    def set_cherry_pick_in_progress(self) -> None:
        return self.set_check_run_status(check_run=CHERRY_PICKED_LABEL_PREFIX, status=IN_PROGRESS_STR)

    def set_cherry_pick_success(self, output: dict[str, Any]) -> None:
        return self.set_check_run_status(check_run=CHERRY_PICKED_LABEL_PREFIX, conclusion=SUCCESS_STR, output=output)

    def set_cherry_pick_failure(self, output: dict[str, Any]) -> None:
        return self.set_check_run_status(check_run=CHERRY_PICKED_LABEL_PREFIX, conclusion=FAILURE_STR, output=output)

    def create_issue_for_new_pull_request(self) -> None:
        if not self.repository:
            raise RepositoryNotInitializedError(f"{self.log_prefix} Repository is not initialized")

        if self.parent_committer in self.auto_verified_and_merged_users:
            self.logger.info(
                f"{self.log_prefix} Committer {self.parent_committer} is part of "
                f"{self.auto_verified_and_merged_users}, will not create issue."
            )
            return

        self.logger.info(f"{self.log_prefix} Creating issue for new PR: {self.pull_request.title}")
        self.repository.create_issue(
            title=self._generate_issue_title(),
            body=self._generate_issue_body(),
            assignee=self.pull_request.user.login,
        )

    def close_issue_for_merged_or_closed_pr(self, hook_action: str) -> None:
        if not self.repository:
            raise RepositoryNotInitializedError(f"{self.log_prefix} Repository is not initialized")

        for issue in self.repository.get_issues():
            if issue.body == self._generate_issue_body():
                self.logger.info(f"{self.log_prefix} Closing issue {issue.title} for PR: {self.pull_request.title}")
                issue.create_comment(
                    f"{self.log_prefix} Closing issue for PR: {self.pull_request.title}.\nPR was {hook_action}."
                )
                issue.edit(state="closed")
                break

    def delete_remote_tag_for_merged_or_closed_pr(self) -> None:
        if not self.build_and_push_container:
            self.logger.info(f"{self.log_prefix} repository do not have container configured")
            return

        repository_full_tag = self._container_repository_and_tag()
        if not repository_full_tag:
            return

        pr_tag = repository_full_tag.split(":")[-1]
        registry_info = self.container_repository.split("/")
        registry_url = "" if len(registry_info) < 3 else registry_info[0]

        reg_login_cmd = f"regctl registry login {registry_url} -u {self.container_repository_username} -p {self.container_repository_password}"
        rc, out, err = self.run_podman_command(command=reg_login_cmd)

        if rc:
            tag_ls_cmd = f"regctl tag ls {self.container_repository} --include {pr_tag}"
            rc, out, err = self.run_podman_command(command=tag_ls_cmd)

            if rc and out:
                tag_del_cmd = f"regctl tag delete {repository_full_tag}"

                if self.run_podman_command(command=tag_del_cmd)[0]:
                    self.pull_request.create_issue_comment(f"Successfully removed PR tag: {repository_full_tag}.")
                else:
                    self.logger.error(
                        f"{self.log_prefix} Failed to delete tag: {repository_full_tag}. OUT:{out}. ERR:{err}"
                    )
            else:
                self.logger.warning(
                    f"{self.log_prefix} {pr_tag} tag not found in registry {self.container_repository}. "
                    f"OUT:{out}. ERR:{err}"
                )
        else:
            self.pull_request.create_issue_comment(
                f"Failed to delete tag: {repository_full_tag}. Please delete it manually."
            )
            self.logger.error(f"{self.log_prefix} Failed to delete tag: {repository_full_tag}. OUT:{out}. ERR:{err}")

    def process_comment_webhook_data(self) -> None:
        if comment_action := self.hook_data["action"] in ("edited", "deleted"):
            self.logger.debug(f"{self.log_prefix} Not processing comment. action is {comment_action}")
            return

        self.logger.info(f"{self.log_prefix} Processing issue {self.hook_data['issue']['number']}")

        body: str = self.hook_data["comment"]["body"]

        if body == self.welcome_msg:
            self.logger.debug(
                f"{self.log_prefix} Welcome message found in issue {self.pull_request.title}. Not processing"
            )
            return

        _user_commands: list[str] = [_cmd.strip("/") for _cmd in body.strip().splitlines() if _cmd.startswith("/")]

        user_login: str = self.hook_data["sender"]["login"]
        for user_command in _user_commands:
            self.user_commands(
                command=user_command,
                reviewed_user=user_login,
                issue_comment_id=self.hook_data["comment"]["id"],
            )

    def process_pull_request_webhook_data(self) -> None:
        hook_action: str = self.hook_data["action"]
        self.logger.info(f"{self.log_prefix} hook_action is: {hook_action}")

        pull_request_data: dict[str, Any] = self.hook_data["pull_request"]
        self.parent_committer = pull_request_data["user"]["login"]
        self.pull_request_branch = pull_request_data["base"]["ref"]
        if self.conventional_title:
            self.set_conventional_title_queued()
            self.conventional_title_check()

        if hook_action == "edited":
            self.set_wip_label_based_on_title()

        if hook_action == "opened":
            self.logger.info(f"{self.log_prefix} Creating welcome comment")
            pull_request_opened_futures: list[Future] = []
            with ThreadPoolExecutor() as executor:
                pull_request_opened_futures.append(
                    executor.submit(self.pull_request.create_issue_comment, **{"body": self.welcome_msg})
                )
                pull_request_opened_futures.append(executor.submit(self.create_issue_for_new_pull_request))
                pull_request_opened_futures.append(executor.submit(self.set_wip_label_based_on_title))
                pull_request_opened_futures.append(executor.submit(self.process_opened_or_synchronize_pull_request))
                if self.jira_track_pr:
                    pull_request_opened_futures.append(executor.submit(self.create_jira_when_open_pull_reques))

                pull_request_opened_futures.append(executor.submit(self.set_pull_request_automerge))

            for result in as_completed(pull_request_opened_futures):
                if _exp := result.exception():
                    self.logger.error(f"{self.log_prefix} {_exp}")

        if hook_action == "synchronize":
            pull_request_synchronize_futures: list[Future] = []
            with ThreadPoolExecutor() as executor:
                pull_request_synchronize_futures.append(executor.submit(self.remove_labels_when_pull_request_sync))
                pull_request_synchronize_futures.append(
                    executor.submit(self.process_opened_or_synchronize_pull_request)
                )

                if self.jira_track_pr:
                    pull_request_synchronize_futures.append(executor.submit(self.update_jira_when_pull_request_sync))

            for result in as_completed(pull_request_synchronize_futures):
                if _exp := result.exception():
                    self.logger.error(f"{self.log_prefix} {_exp}")

        if hook_action == "closed":
            self.close_issue_for_merged_or_closed_pr(hook_action=hook_action)
            self.delete_remote_tag_for_merged_or_closed_pr()
            if is_merged := pull_request_data.get("merged", False):
                self.logger.info(f"{self.log_prefix} PR is merged")

                for _label in self.pull_request.labels:
                    _label_name = _label.name
                    if _label_name.startswith(CHERRY_PICK_LABEL_PREFIX):
                        self.cherry_pick(target_branch=_label_name.replace(CHERRY_PICK_LABEL_PREFIX, ""))

                self._run_build_container(
                    push=True,
                    set_check=False,
                    is_merged=is_merged,
                )

                # label_by_pull_requests_merge_state_after_merged will override self.pull_request
                original_pull_request = self.pull_request
                self.label_all_opened_pull_requests_merge_state_after_merged()
                self.pull_request = original_pull_request

            if self.jira_track_pr:
                self.close_jira_when_pull_request_closed(is_merged=is_merged)

        if hook_action in ("labeled", "unlabeled"):
            _check_for_merge: bool = False
            _reviewer: str | None = None
            action_labeled = hook_action == "labeled"
            labeled = self.hook_data["label"]["name"].lower()
            if labeled == CAN_BE_MERGED_STR:
                return

            self.logger.info(f"{self.log_prefix} PR {self.pull_request.number} {hook_action} with {labeled}")
            if labeled.startswith(APPROVED_BY_LABEL_PREFIX):
                _reviewer = labeled.split(APPROVED_BY_LABEL_PREFIX)[-1]

            if labeled.startswith(CHANGED_REQUESTED_BY_LABEL_PREFIX):
                _reviewer = labeled.split(CHANGED_REQUESTED_BY_LABEL_PREFIX)[-1]

            _approved_output: dict[str, Any] = {"title": "Approved", "summary": "", "text": ""}
            if _reviewer in self.all_approvers:
                _check_for_merge = True
                _approved_output["text"] += f"Approved by {_reviewer}.\n"

            if self.verified_job and labeled == VERIFIED_LABEL_STR:
                _check_for_merge = True
                if action_labeled:
                    self.set_verify_check_success()
                else:
                    self.set_verify_check_queued()

            if _check_for_merge:
                self.check_if_can_be_merged()

    def process_push_webhook_data(self) -> None:
        tag = re.search(r"refs/tags/?(.*)", self.hook_data["ref"])
        if tag:
            tag_name = tag.group(1)
            self.logger.info(f"{self.log_prefix} Processing push for tag: {tag.group(1)}")
            if self.pypi:
                self.logger.info(f"{self.log_prefix} Processing upload to pypi for tag: {tag_name}")
                self.upload_to_pypi(tag_name=tag_name)

            if self.build_and_push_container and self.container_release:
                self.logger.info(f"{self.log_prefix} Processing build and push container for tag: {tag_name}")
                self._run_build_container(push=True, set_check=False, tag=tag_name)

    def process_pull_request_review_webhook_data(self) -> None:
        if self.hook_data["action"] == "submitted":
            """
            Available actions:
                commented
                approved
                changes_requested
            """
            reviewed_user = self.hook_data["review"]["user"]["login"]

            review_state = self.hook_data["review"]["state"]
            self.manage_reviewed_by_label(
                review_state=review_state,
                action=ADD_STR,
                reviewed_user=reviewed_user,
            )

            if self.jira_track_pr:
                self.update_jira_when_pull_request_updated(reviewed_user=reviewed_user, review_state=review_state)

    def manage_reviewed_by_label(self, review_state: str, action: str, reviewed_user: str) -> None:
        self.logger.info(
            f"{self.log_prefix} "
            f"Processing label for review from {reviewed_user}. "
            f"review_state: {review_state}, action: {action}"
        )
        label_prefix: str = ""
        label_to_remove: str = ""

        if reviewed_user in self.all_approvers:
            approved_lgtm_label = APPROVED_BY_LABEL_PREFIX
        else:
            approved_lgtm_label = LGTM_BY_LABEL_PREFIX

        if review_state in ("approved", LGTM_STR):
            base_dict = self.hook_data.get("issue", self.hook_data.get("pull_request"))
            pr_owner = base_dict["user"]["login"]
            if pr_owner == reviewed_user:
                self.logger.info(f"{self.log_prefix} PR owner {pr_owner} set /lgtm, not adding label.")
                return

            _remove_label = f"{CHANGED_REQUESTED_BY_LABEL_PREFIX}{reviewed_user}"
            label_prefix = approved_lgtm_label
            label_to_remove = _remove_label

        elif review_state == "changes_requested":
            label_prefix = CHANGED_REQUESTED_BY_LABEL_PREFIX
            _remove_label = approved_lgtm_label
            label_to_remove = _remove_label

        elif review_state == "commented":
            label_prefix = COMMENTED_BY_LABEL_PREFIX

        if label_prefix:
            reviewer_label = f"{label_prefix}{reviewed_user}"

            if action == ADD_STR:
                self._add_label(label=reviewer_label)
                self._remove_label(label=label_to_remove)

            if action == DELETE_STR:
                self._remove_label(label=reviewer_label)
        else:
            self.logger.warning(
                f"{self.log_prefix} PR {self.pull_request.number} got unsupported review state: {review_state}"
            )

    def _run_tox(self) -> None:
        if not self.tox:
            return

        if self.is_check_run_in_progress(check_run=TOX_STR):
            self.logger.debug(f"{self.log_prefix} Check run is in progress, not running {TOX_STR}.")
            return

        clone_repo_dir = f"{self.clone_repo_dir}-{uuid4()}"
        python_ver = f"--python={self.tox_python_version}" if self.tox_python_version else ""
        cmd = f"uvx {python_ver} {TOX_STR} --workdir {clone_repo_dir} --root {clone_repo_dir} -c {clone_repo_dir}"
        _tox_tests = self.tox.get(self.pull_request_branch, "")
        if _tox_tests and _tox_tests != "all":
            tests = _tox_tests.replace(" ", "")
            cmd += f" -e {tests}"

        self.set_run_tox_check_in_progress()
        with self._prepare_cloned_repo_dir(clone_repo_dir=clone_repo_dir):
            rc, out, err = run_command(command=cmd, log_prefix=self.log_prefix)

            output: dict[str, Any] = {
                "title": "Tox",
                "summary": "",
                "text": self.get_check_run_text(err=err, out=out),
            }
            if rc:
                return self.set_run_tox_check_success(output=output)
            else:
                return self.set_run_tox_check_failure(output=output)

    def _run_pre_commit(self) -> None:
        if not self.pre_commit:
            return

        if self.is_check_run_in_progress(check_run=PRE_COMMIT_STR):
            self.logger.debug(f"{self.log_prefix} Check run is in progress, not running {PRE_COMMIT_STR}.")
            return

        clone_repo_dir = f"{self.clone_repo_dir}-{uuid4()}"
        cmd = f" uvx --directory {clone_repo_dir} {PRE_COMMIT_STR} run --all-files"
        self.set_run_pre_commit_check_in_progress()
        with self._prepare_cloned_repo_dir(clone_repo_dir=clone_repo_dir):
            rc, out, err = run_command(command=cmd, log_prefix=self.log_prefix)

            output: dict[str, Any] = {
                "title": "Pre-Commit",
                "summary": "",
                "text": self.get_check_run_text(err=err, out=out),
            }
            if rc:
                return self.set_run_pre_commit_check_success(output=output)
            else:
                return self.set_run_pre_commit_check_failure(output=output)

    def user_commands(self, command: str, reviewed_user: str, issue_comment_id: int) -> None:
        self.create_comment_reaction(issue_comment_id=issue_comment_id, reaction=REACTIONS.ok)

        available_commands: list[str] = [
            COMMAND_RETEST_STR,
            COMMAND_CHERRY_PICK_STR,
            COMMAND_ASSIGN_REVIEWERS_STR,
            COMMAND_CHECK_CAN_MERGE_STR,
            BUILD_AND_PUSH_CONTAINER_STR,
            COMMAND_ASSIGN_REVIEWER_STR,
        ]

        command_and_args: list[str] = command.split(" ", 1)
        _command = command_and_args[0]
        _args: str = command_and_args[1] if len(command_and_args) > 1 else ""

        self.logger.debug(
            f"{self.log_prefix} User: {reviewed_user}, Command: {_command}, Command args: {_args if _args else 'None'}"
        )
        if _command not in available_commands + list(USER_LABELS_DICT.keys()):
            self.logger.debug(f"{self.log_prefix} Command {command} is not supported.")
            return

        self.logger.info(f"{self.log_prefix} Processing label/user command {command} by user {reviewed_user}")

        if remove := len(command_and_args) > 1 and _args == "cancel":
            self.logger.debug(f"{self.log_prefix} User requested 'cancel' for command {_command}")

        if _command in (COMMAND_RETEST_STR, COMMAND_ASSIGN_REVIEWER_STR) and not _args:
            missing_command_arg_comment_msg: str = f"{_command} requires an argument"
            error_msg: str = f"{self.log_prefix} {missing_command_arg_comment_msg}"
            self.logger.debug(error_msg)
            self.pull_request.create_issue_comment(missing_command_arg_comment_msg)
            return

        if _command == COMMAND_ASSIGN_REVIEWER_STR:
            self._add_reviewer_by_user_comment(reviewer=_args)

        elif _command == COMMAND_ASSIGN_REVIEWERS_STR:
            self.assign_reviewers()

        elif _command == COMMAND_CHECK_CAN_MERGE_STR:
            self.check_if_can_be_merged()

        elif _command == COMMAND_CHERRY_PICK_STR:
            self.process_cherry_pick_command(command_args=_args, reviewed_user=reviewed_user)

        elif _command == COMMAND_RETEST_STR:
            self.process_retest_command(command_args=_args)

        elif _command == BUILD_AND_PUSH_CONTAINER_STR:
            if self.build_and_push_container:
                self._run_build_container(push=True, set_check=False, command_args=_args)
            else:
                msg = f"No {BUILD_AND_PUSH_CONTAINER_STR} configured for this repository"
                error_msg = f"{self.log_prefix} {msg}"
                self.logger.debug(error_msg)
                self.pull_request.create_issue_comment(msg)

        elif _command == WIP_STR:
            wip_for_title: str = f"{WIP_STR.upper()}:"
            if remove:
                self._remove_label(label=WIP_STR)
                self.pull_request.edit(title=self.pull_request.title.replace(wip_for_title, ""))
            else:
                self._add_label(label=WIP_STR)
                self.pull_request.edit(title=f"{wip_for_title} {self.pull_request.title}")

        elif _command == HOLD_LABEL_STR:
            if reviewed_user not in self.all_approvers:
                self.pull_request.create_issue_comment(
                    f"{reviewed_user} is not part of the approver, only approvers can mark pull request as hold"
                )
            else:
                if remove:
                    self._remove_label(label=HOLD_LABEL_STR)
                else:
                    self._add_label(label=HOLD_LABEL_STR)

                self.check_if_can_be_merged()

        elif _command == VERIFIED_LABEL_STR:
            if remove:
                self._remove_label(label=VERIFIED_LABEL_STR)
                self.set_verify_check_queued()
            else:
                self._add_label(label=VERIFIED_LABEL_STR)
                self.set_verify_check_success()

        else:
            self.label_by_user_comment(
                user_requested_label=_command,
                remove=remove,
                reviewed_user=reviewed_user,
            )

    def cherry_pick(self, target_branch: str, reviewed_user: str = "") -> None:
        requested_by = reviewed_user or "by target-branch label"
        self.logger.info(f"{self.log_prefix} Cherry-pick requested by user: {requested_by}")

        new_branch_name = f"{CHERRY_PICKED_LABEL_PREFIX}-{self.pull_request.head.ref}-{shortuuid.uuid()[:5]}"
        if not self.is_branch_exists(branch=target_branch):
            err_msg = f"cherry-pick failed: {target_branch} does not exists"
            self.logger.error(err_msg)
            self.pull_request.create_issue_comment(err_msg)

        else:
            self.set_cherry_pick_in_progress()
            commit_hash = self.pull_request.merge_commit_sha
            commit_msg_striped = self.pull_request.title.replace("'", "")
            pull_request_url = self.pull_request.html_url
            clone_repo_dir = f"{self.clone_repo_dir}-{uuid4()}"
            git_cmd = f"git --work-tree={clone_repo_dir} --git-dir={clone_repo_dir}/.git"
            hub_cmd = f"GITHUB_TOKEN={self.token} hub --work-tree={clone_repo_dir} --git-dir={clone_repo_dir}/.git"
            commands: list[str] = [
                f"{git_cmd} checkout {target_branch}",
                f"{git_cmd} pull origin {target_branch}",
                f"{git_cmd} checkout -b {new_branch_name} origin/{target_branch}",
                f"{git_cmd} cherry-pick {commit_hash}",
                f"{git_cmd} push origin {new_branch_name}",
                f"bash -c \"{hub_cmd} pull-request -b {target_branch} -h {new_branch_name} -l {CHERRY_PICKED_LABEL_PREFIX} -m '{CHERRY_PICKED_LABEL_PREFIX}: [{target_branch}] {commit_msg_striped}' -m 'cherry-pick {pull_request_url} into {target_branch}' -m 'requested-by {requested_by}'\"",
            ]

            rc, out, err = None, "", ""
            with self._prepare_cloned_repo_dir(clone_repo_dir=clone_repo_dir):
                for cmd in commands:
                    rc, out, err = run_command(command=cmd, log_prefix=self.log_prefix)
                    if not rc:
                        output = {
                            "title": "Cherry-pick details",
                            "summary": "",
                            "text": self.get_check_run_text(err=err, out=out),
                        }
                        self.set_cherry_pick_failure(output=output)
                        self.logger.error(f"{self.log_prefix} Cherry pick failed: {out} --- {err}")
                        local_branch_name = f"{self.pull_request.head.ref}-{target_branch}"
                        self.pull_request.create_issue_comment(
                            f"**Manual cherry-pick is needed**\nCherry pick failed for "
                            f"{commit_hash} to {target_branch}:\n"
                            f"To cherry-pick run:\n"
                            "```\n"
                            f"git remote update\n"
                            f"git checkout {target_branch}\n"
                            f"git pull origin {target_branch}\n"
                            f"git checkout -b {local_branch_name}\n"
                            f"git cherry-pick {commit_hash}\n"
                            f"git push origin {local_branch_name}\n"
                            "```"
                        )
                        return

            output = {
                "title": "Cherry-pick details",
                "summary": "",
                "text": self.get_check_run_text(err=err, out=out),
            }
            self.set_cherry_pick_success(output=output)
            self.pull_request.create_issue_comment(f"Cherry-picked PR {self.pull_request.title} into {target_branch}")

    def label_all_opened_pull_requests_merge_state_after_merged(self) -> None:
        """
        Labels pull requests based on their mergeable state.

        If the mergeable state is 'behind', the 'needs rebase' label is added.
        If the mergeable state is 'dirty', the 'has conflicts' label is added.
        """
        if not self.repository:
            raise RepositoryNotInitializedError(f"{self.log_prefix} Repository is not initialized")

        time_sleep = 30
        self.logger.info(f"{self.log_prefix} Sleep for {time_sleep} seconds before getting all opened PRs")
        time.sleep(time_sleep)

        for pull_request in self.repository.get_pulls(state="open"):
            self.pull_request = pull_request
            self.logger.info(f"{self.log_prefix} check label pull request after merge")
            self.label_pull_request_by_merge_state()

    def label_pull_request_by_merge_state(self) -> None:
        merge_state = self.pull_request.mergeable_state
        self.logger.debug(f"{self.log_prefix} Mergeable state is {merge_state}")
        if merge_state == "unknown":
            return

        if merge_state == "behind":
            self._add_label(label=NEEDS_REBASE_LABEL_STR)
        else:
            self._remove_label(label=NEEDS_REBASE_LABEL_STR)

        if merge_state == "dirty":
            self._add_label(label=HAS_CONFLICTS_LABEL_STR)
        else:
            self._remove_label(label=HAS_CONFLICTS_LABEL_STR)

    def check_if_can_be_merged(self) -> None:
        """
        Check if PR can be merged and set the job for it

        Check the following:
            None of the required status checks in progress.
            Has verified label.
            Has approved from one of the approvers.
            All required run check passed.
            PR status is not 'dirty'.
            PR has no changed requests from approvers.
        """
        if self.skip_if_pull_request_already_merged():
            self.logger.debug(f"{self.log_prefix} Pull request already merged")
            return

        output = {
            "title": "Check if can be merged",
            "summary": "",
            "text": None,
        }
        failure_output = ""

        try:
            self.logger.info(f"{self.log_prefix} Check if {CAN_BE_MERGED_STR}.")
            self.set_merge_check_queued()
            last_commit_check_runs = list(self.last_commit.get_check_runs())
            _labels = self.pull_request_labels_names()
            self.logger.debug(f"{self.log_prefix} check if can be merged. PR labels are: {_labels}")

            is_pr_mergable = self.pull_request.mergeable
            if not is_pr_mergable:
                failure_output += f"PR is not mergeable: {is_pr_mergable}\n"

            required_check_in_progress_failure_output, check_runs_in_progress = self._required_check_in_progress(
                last_commit_check_runs=last_commit_check_runs
            )
            if required_check_in_progress_failure_output:
                failure_output += required_check_in_progress_failure_output

            labels_failure_output = self._wip_or_hold_lables_exists(labels=_labels)
            if labels_failure_output:
                failure_output += labels_failure_output

            required_check_failed_failure_output = self._required_check_failed(
                last_commit_check_runs=last_commit_check_runs, check_runs_in_progress=check_runs_in_progress
            )
            if required_check_failed_failure_output:
                failure_output += required_check_failed_failure_output

            lables_failue_output = self._check_lables_for_can_be_merged(labels=_labels)
            if lables_failue_output:
                failure_output += lables_failue_output

            pr_approvered_failure_output = self._check_if_pr_approved(labels=_labels)
            if pr_approvered_failure_output:
                failure_output += pr_approvered_failure_output

            if not failure_output:
                self._add_label(label=CAN_BE_MERGED_STR)
                self.set_merge_check_success()

                self.logger.info(f"{self.log_prefix} Pull request can be merged")
                return

            self.logger.debug(f"{self.log_prefix} cannot be merged: {failure_output}")
            output["text"] = failure_output
            self._remove_label(label=CAN_BE_MERGED_STR)
            self.set_merge_check_failure(output=output)

        except Exception as ex:
            self.logger.error(
                f"{self.log_prefix} Failed to check if can be merged, set check run to {FAILURE_STR} {ex}"
            )
            _err = "Failed to check if can be merged, check logs"
            output["text"] = _err
            self._remove_label(label=CAN_BE_MERGED_STR)
            self.set_merge_check_failure(output=output)

    @staticmethod
    def _comment_with_details(title: str, body: str) -> str:
        return f"""
<details>
<summary>{title}</summary>
    {body}
</details>
        """

    def _container_repository_and_tag(self, is_merged: bool = False, tag: str = "") -> str:
        if not tag:
            if is_merged:
                tag = (
                    self.pull_request_branch
                    if self.pull_request_branch not in (OTHER_MAIN_BRANCH, "main")
                    else self.container_tag
                )
            else:
                if self.pull_request:
                    tag = f"pr-{self.pull_request.number}"

        if tag:
            self.logger.debug(f"{self.log_prefix} container tag is: {tag}")
            return f"{self.container_repository}:{tag}"

        self.logger.error(f"{self.log_prefix} container tag not found")
        return f"{self.container_repository}:webhook-server-tag-not-found"

    def _run_build_container(
        self,
        set_check: bool = True,
        push: bool = False,
        is_merged: bool = False,
        tag: str = "",
        command_args: str = "",
    ) -> None:
        if not self.build_and_push_container:
            return

        clone_repo_dir = f"{self.clone_repo_dir}-{uuid4()}"
        pull_request = hasattr(self, "pull_request")

        if pull_request and set_check:
            if self.is_check_run_in_progress(check_run=BUILD_CONTAINER_STR) and not is_merged:
                self.logger.info(f"{self.log_prefix} Check run is in progress, not running {BUILD_CONTAINER_STR}.")
                return

            self.set_container_build_in_progress()

        _container_repository_and_tag = self._container_repository_and_tag(is_merged=is_merged, tag=tag)
        no_cache: str = " --no-cache" if is_merged else ""
        build_cmd: str = f"--network=host {no_cache} -f {clone_repo_dir}/{self.dockerfile} {clone_repo_dir} -t {_container_repository_and_tag}"

        if self.container_build_args:
            build_args: str = [f"--build-arg {b_arg}" for b_arg in self.container_build_args][0]
            build_cmd = f"{build_args} {build_cmd}"

        if self.container_command_args:
            build_cmd = f"{' '.join(self.container_command_args)} {build_cmd}"

        if command_args:
            build_cmd = f"{command_args} {build_cmd}"

        podman_build_cmd: str = f"podman build {build_cmd}"
        with self._prepare_cloned_repo_dir(
            is_merged=is_merged,
            tag_name=tag,
            clone_repo_dir=clone_repo_dir,
        ):
            build_rc, build_out, build_err = self.run_podman_command(command=podman_build_cmd, pipe=True)
            output: dict[str, str] = {
                "title": "Build container",
                "summary": "",
                "text": self.get_check_run_text(err=build_err, out=build_out),
            }

            if build_rc:
                self.logger.info(f"{self.log_prefix} Done building {_container_repository_and_tag}")
                if pull_request and set_check:
                    return self.set_container_build_success(output=output)
            else:
                self.logger.error(f"{self.log_prefix} Failed to build {_container_repository_and_tag}")
                if self.pull_request and set_check:
                    return self.set_container_build_failure(output=output)

            if push and build_rc:
                cmd = f"podman push --creds {self.container_repository_username}:{self.container_repository_password} {_container_repository_and_tag}"
                push_rc, _, _ = self.run_podman_command(command=cmd)
                if push_rc:
                    push_msg: str = f"New container for {_container_repository_and_tag} published"
                    if pull_request:
                        self.pull_request.create_issue_comment(push_msg)

                    if self.slack_webhook_url:
                        message = f"""
```
{self.repository_full_name} {push_msg}.
```
"""
                        self.send_slack_message(message=message, webhook_url=self.slack_webhook_url)

                    self.logger.info(f"{self.log_prefix} Done push {_container_repository_and_tag}")
                else:
                    err_msg: str = f"Failed to build and push {_container_repository_and_tag}"
                    if self.pull_request:
                        self.pull_request.create_issue_comment(err_msg)

                    if self.slack_webhook_url:
                        message = f"""
```
{self.repository_full_name} {err_msg}.
```
                        """
                        self.send_slack_message(message=message, webhook_url=self.slack_webhook_url)

    def _run_install_python_module(self) -> None:
        if not self.pypi:
            return

        if self.is_check_run_in_progress(check_run=PYTHON_MODULE_INSTALL_STR):
            self.logger.info(f"{self.log_prefix} Check run is in progress, not running {PYTHON_MODULE_INSTALL_STR}.")
            return

        clone_repo_dir = f"{self.clone_repo_dir}-{uuid4()}"
        self.logger.info(f"{self.log_prefix} Installing python module")
        self.set_python_module_install_in_progress()
        with self._prepare_cloned_repo_dir(
            clone_repo_dir=clone_repo_dir,
        ):
            rc, out, err = run_command(
                command=f"uvx pip wheel --no-cache-dir -w {clone_repo_dir}/dist {clone_repo_dir}",
                log_prefix=self.log_prefix,
            )

            output: dict[str, str] = {
                "title": "Python module installation",
                "summary": "",
                "text": self.get_check_run_text(err=err, out=out),
            }
            if rc:
                return self.set_python_module_install_success(output=output)

            return self.set_python_module_install_failure(output=output)

    def send_slack_message(self, message: str, webhook_url: str) -> None:
        slack_data: dict[str, str] = {"text": message}
        self.logger.info(f"{self.log_prefix} Sending message to slack: {message}")
        response: requests.Response = requests.post(
            webhook_url,
            data=json.dumps(slack_data),
            headers={"Content-Type": "application/json"},
        )
        if response.status_code != 200:
            raise ValueError(
                f"Request to slack returned an error {response.status_code} with the following message: {response.text}"
            )

    def _process_verified_for_update_or_new_pull_request(self) -> None:
        if not self.verified_job:
            return

        if self.parent_committer in self.auto_verified_and_merged_users:
            self.logger.info(
                f"{self.log_prefix} Committer {self.parent_committer} is part of {self.auto_verified_and_merged_users}"
                ", Setting verified label"
            )
            self._add_label(label=VERIFIED_LABEL_STR)
            self.set_verify_check_success()
        else:
            self.logger.info(f"{self.log_prefix} Processing reset {VERIFIED_LABEL_STR} label on new commit push")
            # Remove verified label
            self._remove_label(label=VERIFIED_LABEL_STR)
            self.set_verify_check_queued()

    def create_comment_reaction(self, issue_comment_id: int, reaction: str) -> None:
        _comment = self.pull_request.get_issue_comment(issue_comment_id)
        _comment.create_reaction(reaction)

    def process_opened_or_synchronize_pull_request(self) -> None:
        prepare_pull_futures: list[Future] = []
        with ThreadPoolExecutor() as executor:
            prepare_pull_futures.append(executor.submit(self.assign_reviewers))
            prepare_pull_futures.append(
                executor.submit(
                    self._add_label,
                    **{"label": f"{BRANCH_LABEL_PREFIX}{self.pull_request_branch}"},
                )
            )
            prepare_pull_futures.append(executor.submit(self.label_pull_request_by_merge_state))
            prepare_pull_futures.append(executor.submit(self.set_merge_check_queued))
            prepare_pull_futures.append(executor.submit(self.set_run_tox_check_queued))
            prepare_pull_futures.append(executor.submit(self.set_run_pre_commit_check_queued))
            prepare_pull_futures.append(executor.submit(self.set_python_module_install_queued))
            prepare_pull_futures.append(executor.submit(self.set_container_build_queued))
            prepare_pull_futures.append(executor.submit(self._process_verified_for_update_or_new_pull_request))
            prepare_pull_futures.append(executor.submit(self.add_size_label))
            prepare_pull_futures.append(executor.submit(self.add_pull_request_owner_as_assingee))

            prepare_pull_futures.append(executor.submit(self._run_tox))
            prepare_pull_futures.append(executor.submit(self._run_pre_commit))
            prepare_pull_futures.append(executor.submit(self._run_install_python_module))
            prepare_pull_futures.append(executor.submit(self._run_build_container))

        for result in as_completed(prepare_pull_futures):
            if _exp := result.exception():
                self.logger.error(f"{self.log_prefix} {_exp}")

    def is_check_run_in_progress(self, check_run: str) -> bool:
        for run in self.last_commit.get_check_runs():
            if run.name == check_run and run.status == IN_PROGRESS_STR:
                return True
        return False

    def set_check_run_status(
        self,
        check_run: str,
        status: str = "",
        conclusion: str = "",
        output: dict[str, str] | None = None,
    ) -> None:
        kwargs: dict[str, Any] = {"name": check_run, "head_sha": self.last_commit.sha}

        if status:
            kwargs["status"] = status

        if conclusion:
            kwargs["conclusion"] = conclusion

        if output:
            kwargs["output"] = output

        msg: str = f"{self.log_prefix} check run {check_run} status: {status or conclusion}"

        try:
            self.repository_by_github_app.create_check_run(**kwargs)
            if conclusion in (SUCCESS_STR, IN_PROGRESS_STR):
                self.logger.success(msg)  # type: ignore
            return

        except Exception as ex:
            self.logger.debug(f"{self.log_prefix} Failed to set {check_run} check to {status or conclusion}, {ex}")
            kwargs["conclusion"] = FAILURE_STR
            self.repository_by_github_app.create_check_run(**kwargs)

    @contextlib.contextmanager
    def _prepare_cloned_repo_dir(
        self,
        clone_repo_dir: str,
        is_merged: bool = False,
        checkout: str = "",
        tag_name: str = "",
    ) -> Generator[None, None, None]:
        if not self.repository:
            raise RepositoryNotInitializedError(f"{self.log_prefix} Repository is not initialized")

        git_cmd = f"git --work-tree={clone_repo_dir} --git-dir={clone_repo_dir}/.git"

        # Clone the repository
        run_command(
            command=f"git clone {self.repository.clone_url.replace('https://', f'https://{self.token}@')} "
            f"{clone_repo_dir}",
            log_prefix=self.log_prefix,
        )
        try:
            run_command(
                command=f"{git_cmd} config user.name '{self.repository.owner.login}'", log_prefix=self.log_prefix
            )
            run_command(f"{git_cmd} config user.email '{self.repository.owner.email}'", log_prefix=self.log_prefix)
            run_command(
                command=f"{git_cmd} config --local --add remote.origin.fetch +refs/pull/*/head:refs/remotes/origin/pr/*",
                log_prefix=self.log_prefix,
            )
            run_command(command=f"{git_cmd} remote update", log_prefix=self.log_prefix)

            # Checkout to requested branch/tag
            if checkout:
                run_command(f"{git_cmd} checkout {checkout}", log_prefix=self.log_prefix)

            # Checkout the branch if pull request is merged or for release
            else:
                if is_merged:
                    run_command(command=f"{git_cmd} checkout {self.pull_request_branch}", log_prefix=self.log_prefix)

                elif tag_name:
                    run_command(command=f"{git_cmd} checkout {tag_name}", log_prefix=self.log_prefix)

                # Checkout the pull request
                else:
                    try:
                        pull_request = self._get_pull_request()
                    except NoPullRequestError:
                        self.logger.error(f"{self.log_prefix} [func:_run_in_container] No pull request found")
                        return

                    run_command(
                        command=f"{git_cmd} checkout origin/pr/{pull_request.number}", log_prefix=self.log_prefix
                    )

            yield

        finally:
            self.logger.debug(f"{self.log_prefix} Deleting {clone_repo_dir}")
            shutil.rmtree(clone_repo_dir)

    @staticmethod
    def get_check_run_text(err: str, out: str) -> str:
        total_len: int = len(err) + len(out)
        if total_len > 65534:  # GitHub limit is 65535 characters
            return f"```\n{err}\n\n{out}\n```"[:65534]
        else:
            return f"```\n{err}\n\n{out}\n```"

    def get_jira_conn(self) -> JiraApi:
        return JiraApi(
            server=self.jira_server,
            project=self.jira_project,
            token=self.jira_token,
        )

    def get_story_key_with_jira_connection(self) -> str:
        _story_label = [_label for _label in self.pull_request.labels if _label.name.startswith(JIRA_STR)]
        if not _story_label:
            return ""

        if _story_key := _story_label[0].name.split(":")[-1]:
            jira_conn = self.get_jira_conn()
            if not jira_conn:
                self.logger.error(f"{self.log_prefix} Jira connection not found")
                return ""
        return _story_key

    def get_branch_required_status_checks(self) -> list[str]:
        if not self.repository:
            raise RepositoryNotInitializedError(f"{self.log_prefix} Repository is not initialized")

        if self.repository.private:
            self.logger.info(
                f"{self.log_prefix} Repository is private, skipping getting branch protection required status checks"
            )
            return []

        pull_request_branch = self.repository.get_branch(self.pull_request_branch)
        branch_protection = pull_request_branch.get_protection()
        return branch_protection.required_status_checks.contexts

    def get_all_required_status_checks(self) -> list[str]:
        if not hasattr(self, "pull_request_branch"):
            self.pull_request_branch = self.pull_request.base.ref

        all_required_status_checks: list[str] = []
        branch_required_status_checks = self.get_branch_required_status_checks()
        if self.tox:
            all_required_status_checks.append(TOX_STR)

        if self.verified_job:
            all_required_status_checks.append(VERIFIED_LABEL_STR)

        if self.build_and_push_container:
            all_required_status_checks.append(BUILD_CONTAINER_STR)

        if self.pypi:
            all_required_status_checks.append(PYTHON_MODULE_INSTALL_STR)

        if self.conventional_title:
            all_required_status_checks.append(CONVENTIONAL_TITLE_STR)

        _all_required_status_checks = branch_required_status_checks + all_required_status_checks
        self.logger.debug(f"{self.log_prefix} All required status checks: {_all_required_status_checks}")
        return _all_required_status_checks

    def set_wip_label_based_on_title(self) -> None:
        if self.pull_request.title.lower().startswith(f"{WIP_STR}:"):
            self.logger.debug(
                f"{self.log_prefix} Found {WIP_STR} in {self.pull_request.title}; adding {WIP_STR} label."
            )
            self._add_label(label=WIP_STR)

        else:
            self.logger.debug(
                f"{self.log_prefix} {WIP_STR} not found in {self.pull_request.title}; removing {WIP_STR} label."
            )
            self._remove_label(label=WIP_STR)

    def set_jira_in_pull_request(self) -> None:
        if self.jira_enabled_repository:
            reviewers_and_approvers = self.root_reviewers + self.root_approvers
            if self.parent_committer in reviewers_and_approvers:
                self.jira_assignee = self.jira_user_mapping.get(self.parent_committer)
                self.add_api_users_to_auto_verified_and_merged_users
                if not self.jira_assignee:
                    self.logger.debug(
                        f"{self.log_prefix} Jira tracking is disabled for the current pull request. "
                        f"Committer {self.parent_committer} is not in configures in jira-user-mapping"
                    )
                else:
                    self.jira_track_pr = True
                    self.issue_title = (
                        f"[AUTO:FROM:GITHUB] [{self.repository_name}] "
                        f"PR [{self.pull_request.number}]: {self.pull_request.title}"
                    )
                    self.logger.debug(f"{self.log_prefix} Jira tracking is enabled for the current pull request.")
            else:
                self.logger.debug(
                    f"{self.log_prefix} Jira tracking is disabled for the current pull request. "
                    f"Committer {self.parent_committer} is not in {reviewers_and_approvers}"
                )

    def process_cherry_pick_command(self, command_args: str, reviewed_user: str) -> None:
        if not self.repository:
            raise RepositoryNotInitializedError(f"{self.log_prefix} Repository is not initialized")

        _target_branches: list[str] = command_args.split()
        _exits_target_branches: set[str] = set()
        _non_exits_target_branches_msg: str = ""

        for _target_branch in _target_branches:
            try:
                self.repository.get_branch(_target_branch)
            except Exception:
                _non_exits_target_branches_msg += f"Target branch `{_target_branch}` does not exist\n"

            _exits_target_branches.add(_target_branch)

        if _non_exits_target_branches_msg:
            self.logger.info(f"{self.log_prefix} {_non_exits_target_branches_msg}")
            self.pull_request.create_issue_comment(_non_exits_target_branches_msg)

        if _exits_target_branches:
            if not self.pull_request.is_merged():
                cp_labels: list[str] = [
                    f"{CHERRY_PICK_LABEL_PREFIX}{_target_branch}" for _target_branch in _exits_target_branches
                ]
                info_msg: str = f"""
Cherry-pick requested for PR: `{self.pull_request.title}` by user `{reviewed_user}`
Adding label/s `{" ".join([_cp_label for _cp_label in cp_labels])}` for automatic cheery-pick once the PR is merged
"""
                self.logger.info(f"{self.log_prefix} {info_msg}")
                self.pull_request.create_issue_comment(info_msg)
                for _cp_label in cp_labels:
                    self._add_label(label=_cp_label)
            else:
                for _exits_target_branch in _exits_target_branches:
                    self.cherry_pick(
                        target_branch=_exits_target_branch,
                        reviewed_user=reviewed_user,
                    )

    def process_retest_command(self, command_args: str) -> None:
        _target_tests: list[str] = command_args.split()
        _not_supported_retests: list[str] = []
        _supported_retests: list[str] = []
        _retests_to_func_map: dict[str, Callable] = {
            TOX_STR: self._run_tox,
            PRE_COMMIT_STR: self._run_pre_commit,
            BUILD_CONTAINER_STR: self._run_build_container,
            PYTHON_MODULE_INSTALL_STR: self._run_install_python_module,
        }

        if not _target_tests:
            msg = "No test defined to retest"
            error_msg = f"{self.log_prefix} {msg}."
            self.logger.debug(error_msg)
            self.pull_request.create_issue_comment(msg)
            return

        if "all" in command_args:
            if len(_target_tests) > 1:
                msg = "Invalid command. `all` cannot be used with other tests"
                error_msg = f"{self.log_prefix} {msg}."
                self.logger.debug(error_msg)
                self.pull_request.create_issue_comment(msg)
                return

            else:
                _supported_retests = self.current_pull_request_supported_retest

        else:
            for _test in _target_tests:
                if _test in self.current_pull_request_supported_retest:
                    _supported_retests.append(_test)

                else:
                    _not_supported_retests.append(_test)

        if _not_supported_retests:
            msg = f"No {' '.join(_not_supported_retests)} configured for this repository"
            error_msg = f"{self.log_prefix} {msg}."
            self.logger.debug(error_msg)
            self.pull_request.create_issue_comment(msg)

        if _supported_retests:
            _retest_to_exec: list[Future] = []
            with ThreadPoolExecutor() as executor:
                for _test in _supported_retests:
                    _retest_to_exec.append(executor.submit(_retests_to_func_map[_test]))

            for result in as_completed(_retest_to_exec):
                if _exp := result.exception():
                    self.logger.error(f"{self.log_prefix} {_exp}")

    def remove_labels_when_pull_request_sync(self) -> None:
        futures = []
        with ThreadPoolExecutor() as executor:
            for _label in self.pull_request.labels:
                _label_name = _label.name
                if (
                    _label_name.startswith(APPROVED_BY_LABEL_PREFIX)
                    or _label_name.startswith(COMMENTED_BY_LABEL_PREFIX)
                    or _label_name.startswith(CHANGED_REQUESTED_BY_LABEL_PREFIX)
                    or _label_name.startswith(LGTM_BY_LABEL_PREFIX)
                ):
                    futures.append(
                        executor.submit(
                            self._remove_label,
                            **{
                                "label": _label_name,
                            },
                        )
                    )
        for _ in as_completed(futures):
            # wait for all tasks to complete
            pass

    def create_jira_when_open_pull_reques(self) -> None:
        jira_conn = self.get_jira_conn()
        if not jira_conn:
            self.logger.error(f"{self.log_prefix} Jira connection not found")

        else:
            if self.jira_epic and self.jira_assignee:
                self.logger.info(f"{self.log_prefix} Creating Jira story")
                jira_story_key = jira_conn.create_story(
                    title=self.issue_title,
                    body=self.pull_request.html_url,
                    epic_key=self.jira_epic,
                    assignee=self.jira_assignee,
                )
                self._add_label(label=f"{JIRA_STR}:{jira_story_key}")
            else:
                self.logger.warning(f"{self.log_prefix} Jira epic or assignee is not set. Skipping Jira story creation")

    def update_jira_when_pull_request_sync(self) -> None:
        jira_conn = self.get_jira_conn()
        if not jira_conn:
            self.logger.error(f"{self.log_prefix} Jira connection not found")

        else:
            if _story_key := self.get_story_key_with_jira_connection():
                if not self.jira_assignee:
                    self.logger.warning(f"{self.log_prefix} Jira assignee is not set. Skipping sub-task creation")

                else:
                    self.logger.info(f"{self.log_prefix} Creating sub-task for Jira story {_story_key}")
                    jira_conn.create_closed_subtask(
                        title=f"{self.issue_title}: New commit from {self.last_committer}",
                        parent_key=_story_key,
                        assignee=self.jira_assignee,
                        body=f"PR: {self.pull_request.title}, new commit pushed by {self.last_committer}",
                    )

    def close_jira_when_pull_request_closed(self, is_merged: bool) -> None:
        jira_conn = self.get_jira_conn()
        if not jira_conn:
            self.logger.error(f"{self.log_prefix} Jira connection not found")

        else:
            if _story_key := self.get_story_key_with_jira_connection():
                self.logger.info(f"{self.log_prefix} Closing Jira story")
                jira_conn.close_issue(
                    key=_story_key,
                    comment=f"PR: {self.pull_request.title} is closed. Merged: {is_merged}",
                )

    def update_jira_when_pull_request_updated(self, reviewed_user: str, review_state: str) -> None:
        _story_label = [_label for _label in self.pull_request.labels if _label.name.startswith(JIRA_STR)]
        if _story_label:
            if reviewed_user == self.parent_committer or reviewed_user == self.last_committer:
                self.logger.info(
                    f"{self.log_prefix} Skipping Jira review sub-task creation for review by {reviewed_user} which is parent or last committer"
                )
                return

            _story_key = _story_label[0].name.split(":")[-1]
            jira_conn = self.get_jira_conn()
            if not jira_conn:
                self.logger.error(f"{self.log_prefix} Jira connection not found")
                return

            self.logger.info(f"{self.log_prefix} Creating sub-task for Jira story {_story_key}")
            jira_conn.create_closed_subtask(
                title=f"{self.issue_title}: reviewed by: {reviewed_user} - {review_state}",
                parent_key=_story_key,
                assignee=self.jira_user_mapping.get(reviewed_user, self.parent_committer),
                body=f"PR: {self.pull_request.title}, reviewed by: {reviewed_user}",
            )

    def add_pull_request_owner_as_assingee(self) -> None:
        try:
            self.logger.info(f"{self.log_prefix} Adding PR owner as assignee")
            self.pull_request.add_to_assignees()
        except Exception as exp:
            self.logger.debug(f"{self.log_prefix} Exception while adding PR owner as assignee: {exp}")

            if self.root_approvers:
                self.logger.debug(f"{self.log_prefix} Falling back to first approver as assignee")
                self.pull_request.add_to_assignees(self.root_approvers[0])

    def set_pull_request_automerge(self) -> None:
        if self.parent_committer in self.auto_verified_and_merged_users:
            try:
                if not self.pull_request.raw_data.get("auto_merge"):
                    self.logger.info(
                        f"{self.log_prefix} will be merged automatically. owner: {self.parent_committer} "
                        f"is part of auto merge enabled users"
                    )

                    self.pull_request.enable_automerge(merge_method="SQUASH")
                else:
                    self.logger.debug(f"{self.log_prefix} is already set to auto merge")

            except Exception as exp:
                self.logger.error(f"{self.log_prefix} Exception while setting auto merge: {exp}")

    @property
    def _current_pull_request_supported_retest(self) -> list[str]:
        current_pull_request_supported_retest: list[str] = []

        if self.tox:
            current_pull_request_supported_retest.append(TOX_STR)

        if self.build_and_push_container:
            current_pull_request_supported_retest.append(BUILD_CONTAINER_STR)

        if self.pypi:
            current_pull_request_supported_retest.append(PYTHON_MODULE_INSTALL_STR)

        if self.pre_commit:
            current_pull_request_supported_retest.append(PRE_COMMIT_STR)

        return current_pull_request_supported_retest

    def is_podman_bug(self, err: str) -> bool:
        _err = "Error: current system boot ID differs from cached boot ID; an unhandled reboot has occurred"
        return _err in err.strip()

    def fix_podman_bug(self) -> None:
        self.logger.debug(f"{self.log_prefix} Fixing podman bug")
        shutil.rmtree("/tmp/storage-run-1000/containers", ignore_errors=True)
        shutil.rmtree("/tmp/storage-run-1000/libpod/tmp", ignore_errors=True)

    def run_podman_command(self, command: str, pipe: bool = False) -> tuple[bool, str, str]:
        rc, out, err = run_command(command=command, log_prefix=self.log_prefix, pipe=pipe)

        if rc:
            return rc, out, err

        if self.is_podman_bug(err=err):
            self.fix_podman_bug()
            return run_command(command=command, log_prefix=self.log_prefix, pipe=pipe)

        return rc, out, err

    def get_all_approvers_and_reviewers(self) -> dict[str, dict[str, Any]]:
        if not self.repository:
            raise RepositoryNotInitializedError(f"{self.log_prefix} Repository is not initialized")

        # Dictionary mapping OWNERS file paths to their approvers and reviewers
        _owners: dict[str, dict[str, Any]] = {}

        max_owners_files = 1000  # Configurable limit
        owners_count = 0

        tree = self.repository.get_git_tree(self.pull_request_branch, recursive=True)
        for element in tree.tree:
            if element.type == "blob" and element.path.endswith("OWNERS"):
                owners_count += 1
                if owners_count > max_owners_files:
                    self.logger.error(f"{self.log_prefix} Too many OWNERS files (>{max_owners_files})")
                    break

                content_path = element.path
                _path = self.repository.get_contents(content_path)
                if isinstance(_path, list):
                    _path = _path[0]

                try:
                    content = yaml.safe_load(_path.decoded_content)
                    if self._validate_owners_content(content, content_path):
                        parent_path = str(Path(content_path).parent)
                        if not parent_path:
                            parent_path = "."
                        _owners[parent_path] = content

                except yaml.YAMLError as exp:
                    self.logger.error(f"{self.log_prefix} Invalid OWNERS file {content_path}: {exp}")
                    continue

        return _owners

    def get_all_approvers(self) -> list[str]:
        _approvers: list[str] = []
        for list_of_approvers in self.owners_data_for_changed_files().values():
            for _approver in list_of_approvers.get("approvers", []):
                _approvers.append(_approver)

        _approvers.sort()
        return _approvers

    def get_all_reviewers(self) -> list[str]:
        _reviewers: list[str] = []
        for list_of_reviewers in self.owners_data_for_changed_files().values():
            for _approver in list_of_reviewers.get("reviewers", []):
                _reviewers.append(_approver)

        _reviewers.sort()
        return _reviewers

    def owners_data_for_changed_files(self) -> dict[str, dict[str, Any]]:
        data: dict[str, dict[str, Any]] = {}

        changed_folders = {Path(cf).parent for cf in self.changed_files}

        changed_folder_match: list[Path] = []

        require_root_approvers: bool | None = None

        for owners_dir, owners_data in self.all_approvers_and_reviewers.items():
            if owners_dir == ".":
                continue

            _owners_dir = Path(owners_dir)

            for changed_folder in changed_folders:
                if changed_folder == _owners_dir or _owners_dir in changed_folder.parents:
                    data[owners_dir] = owners_data
                    changed_folder_match.append(_owners_dir)
                    if require_root_approvers is None:
                        require_root_approvers = owners_data.get("root-approvers", True)

        if require_root_approvers or require_root_approvers is None:
            data["."] = self.all_approvers_and_reviewers.get(".", {})

        else:
            for _folder in changed_folders:
                for _changed_path in changed_folder_match:
                    if _folder == _changed_path or _changed_path in _folder.parents:
                        continue
                    else:
                        data["."] = self.all_approvers_and_reviewers.get(".", {})
                        break

        self.logger.debug(f"{self.log_prefix} Owners data for current pull request: {yaml.dump(data)}")
        return data

    def _validate_owners_content(self, content: Any, path: str) -> bool:
        """Validate OWNERS file content structure."""
        try:
            if not isinstance(content, dict):
                raise ValueError("OWNERS file must contain a dictionary")

            for key in ["approvers", "reviewers"]:
                if key in content:
                    if not isinstance(content[key], list):
                        raise ValueError(f"{key} must be a list")

                    if not all(isinstance(_elm, str) for _elm in content[key]):
                        raise ValueError(f"All {key} must be strings")

            return True

        except ValueError as e:
            self.logger.error(f"{self.log_prefix} Invalid OWNERS file {path}: {e}")
            return False

    def _required_check_in_progress(self, last_commit_check_runs: list[CheckRun]) -> tuple[str, list[str]]:
        self.all_required_status_checks = self.get_all_required_status_checks()
        last_commit_check_runs = list(self.last_commit.get_check_runs())
        self.logger.debug(f"{self.log_prefix} Check if any required check runs in progress.")
        check_runs_in_progress = [
            check_run.name
            for check_run in last_commit_check_runs
            if check_run.status == IN_PROGRESS_STR
            and check_run.name != CAN_BE_MERGED_STR
            and check_run.name in self.all_required_status_checks
        ]
        if check_runs_in_progress:
            self.logger.debug(
                f"{self.log_prefix} Some required check runs in progress {check_runs_in_progress}, "
                f"skipping check if {CAN_BE_MERGED_STR}."
            )
            return f"Some required check runs in progress {', '.join(check_runs_in_progress)}\n", check_runs_in_progress
        return "", []

    def _required_check_failed(self, last_commit_check_runs: list[CheckRun], check_runs_in_progress: list[str]) -> str:
        failed_check_runs = []
        for check_run in last_commit_check_runs:
            if (
                check_run.name == CAN_BE_MERGED_STR
                or check_run.conclusion == SUCCESS_STR
                or check_run.conclusion == QUEUED_STR
                or check_run.name not in self.all_required_status_checks
            ):
                continue

            failed_check_runs.append(check_run.name)

        if failed_check_runs:
            exclude_in_progress = [
                failed_check_run
                for failed_check_run in failed_check_runs
                if failed_check_run not in check_runs_in_progress
            ]
            return f"Some check runs failed: {', '.join(exclude_in_progress)}\n"

        return ""

    def _wip_or_hold_lables_exists(self, labels: list[str]) -> str:
        failure_output = ""
        is_hold = HOLD_LABEL_STR in labels
        is_wip = WIP_STR in labels

        if is_hold or is_wip:
            if is_hold:
                failure_output += "Hold label exists.\n"

            if is_wip:
                failure_output += "WIP label exists.\n"

        return failure_output

    def _check_lables_for_can_be_merged(self, labels: list[str]) -> str:
        failure_output = ""

        for _label in labels:
            if CHANGED_REQUESTED_BY_LABEL_PREFIX.lower() in _label.lower():
                change_request_user = _label.split("-")[-1]
                if change_request_user in self.all_approvers:
                    failure_output += "PR has changed requests from approvers\n"

        missing_required_labels = []
        for _req_label in self.can_be_merged_required_labels:
            if _req_label not in labels:
                missing_required_labels.append(_req_label)

        if missing_required_labels:
            failure_output += f"Missing required labels: {', '.join(missing_required_labels)}\n"

        return failure_output

    def _check_if_pr_approved(self, labels: list[str]) -> str:
        approved_by = []
        for _label in labels:
            if APPROVED_BY_LABEL_PREFIX.lower() in _label.lower():
                approved_user = _label.split("-")[-1]
                if self.parent_committer == approved_user:
                    continue

                approved_by.append(approved_user)

        missing_approvers = self.all_approvers.copy()

        for data in self.owners_data_for_changed_files().values():
            required_pr_approvers = data.get("approvers", [])
            for required_pr_approver in required_pr_approvers:
                if required_pr_approver in approved_by:
                    # Once we found approver in approved_by list, we remove all approvers from missing_approvers list for this owners file
                    {
                        missing_approvers.remove(_approver)  # type: ignore
                        for _approver in required_pr_approvers
                        if _approver in missing_approvers
                    }
                    break

        missing_approvers = list(set(missing_approvers))
        if self.parent_committer in missing_approvers:
            missing_approvers.remove(self.parent_committer)

        if missing_approvers:
            return f"Missing lgtm/approved from approvers: {', '.join(missing_approvers)}\n"

        return ""

    def _add_reviewer_by_user_comment(self, reviewer: str) -> None:
        if not self.repository:
            raise RepositoryNotInitializedError(f"{self.log_prefix} Repository is not initialized")

        reviewer = reviewer.strip("@")
        self.logger.info(f"{self.log_prefix} Adding reviewer {reviewer} by user comment")

        for contributer in self.repository.get_contributors():
            if contributer.login == reviewer:
                self.pull_request.create_review_request([reviewer])
                return

        _err = f"not adding reviewer {reviewer} by user comment, {reviewer} is not part of contributers"
        self.logger.debug(f"{self.log_prefix} {_err}")
        self.pull_request.create_issue_comment(_err)

    def conventional_title_check(self) -> None:
        output: dict[str, str] = {
            "title": "Conventional Title",
            "summary": "",
            "text": "",
        }
        self.set_conventional_title_in_progress()
        allowed_names = self.conventional_title.split(",")
        title = self.pull_request.title
        if any([title.startswith(f"{_name}:") for _name in allowed_names]):
            self.set_conventional_title_success(output=output)
        else:
            output["summary"] = "Failed"
            output["text"] = f"Pull request title must starts with allowed title: {': ,'.join(allowed_names)}"

            self.set_conventional_title_failure(output=output)
