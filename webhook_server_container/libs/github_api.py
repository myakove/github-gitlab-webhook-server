from __future__ import annotations
import contextlib
from datetime import datetime
import json
import logging
import os
import random
import re
import time
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
from stringcolor import cs

from github.Branch import Branch
from github.ContentFile import ContentFile
import requests
import shortuuid
from starlette.datastructures import Headers
import yaml
from github import GithubException
from github.Commit import Commit
from github.PullRequest import PullRequest
from github.GithubException import UnknownObjectException
from timeout_sampler import TimeoutSampler, TimeoutExpiredError

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
    COMMAND_ASSIGN_REVIEWERS_STR,
    COMMAND_CHECK_CAN_MERGE_STR,
    COMMAND_CHERRY_PICK_STR,
    COMMAND_RETEST_STR,
    COMMENTED_BY_LABEL_PREFIX,
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
    PRE_COMMIT_STR,
    OTHER_MAIN_BRANCH,
)
from webhook_server_container.utils.github_repository_settings import (
    get_repository_github_app_api,
)
from webhook_server_container.utils.helpers import (
    get_api_with_highest_rate_limit,
    extract_key_from_dict,
    get_github_repo_api,
    get_value_from_dicts,
    run_command,
    get_apis_and_tokes_from_config,
)


class NoPullRequestError(Exception):
    pass


class RepositoryNotFoundError(Exception):
    pass


class ProcessGithubWehookError(Exception):
    def __init__(self, err: Dict[str, str]):
        self.err = err

    def __str__(self) -> str:
        return f"{self.err}"


class ProcessGithubWehook:
    def __init__(self, hook_data: Dict[Any, Any], headers: Headers, logger: logging.Logger) -> None:
        self.start_time = datetime.now()
        self.logger = logger
        self.logger.name = "ProcessGithubWehook"
        self.hook_data = hook_data
        self.headers = headers
        self.repository_name: str = hook_data["repository"]["name"]
        self.parent_committer: str = ""
        self.container_repo_dir: str = "/tmp/repository"
        self.jira_track_pr: bool = False
        self.issue_title: str = ""
        self.all_required_status_checks: List[str] = []
        self.x_github_delivery: str = self.headers.get("X-GitHub-Delivery", "")
        self.github_event: str = self.headers["X-GitHub-Event"]
        self.owners_content: Dict[str, Any] = {}

        self.config = Config()
        self.log_prefix = self.prepare_log_prefix()
        self._repo_data_from_config()
        self.logger.debug(f"\n\n{self.log_prefix} *** INIT started ***\n")

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

        self.github_api, self.token = get_api_with_highest_rate_limit(
            config=self.config, repository_name=self.repository_name
        )

        if self.github_api and self.token:
            self.repository = get_github_repo_api(github_api=self.github_api, repository=self.repository_full_name)

        else:
            self.logger.error(f"{self.log_prefix} Failed to get GitHub API and token.")
            return

        self.repository_by_github_app = get_github_repo_api(
            github_api=self.github_app_api, repository=self.repository_full_name
        )

        if not (self.repository or self.repository_by_github_app):
            self.logger.error(f"{self.log_prefix} Failed to get repository.")
            return

        self.add_api_users_to_auto_verified_and_merged_users()
        self.clone_repository_path: str = os.path.join("/", self.repository.name)

        self.supported_user_labels_str: str = "".join([f" * {label}\n" for label in USER_LABELS_DICT.keys()])
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

<details>
<summary>Supported /retest check runs</summary>

{self.prepare_retest_wellcome_msg}
</details>

<details>
<summary>Supported labels</summary>

{self.supported_user_labels_str}
</details>
    """

        self.logger.debug(
            f"\n\n{self.log_prefix} *** INIT ended, took: {(datetime.now() - self.start_time).seconds} seconds ***\n"
        )

    def process(self) -> None:
        if self.github_event == "ping":
            return

        event_log: str = f"Event type: {self.github_event}. event ID: {self.x_github_delivery}"
        self.owners_content = self.get_owners_content()

        try:
            self.pull_request = self._get_pull_request()
            self.log_prefix = self.prepare_log_prefix(pull_request=self.pull_request)
            self.logger.debug(f"{self.log_prefix} {event_log}")
            self.last_commit = self._get_last_commit()
            self.parent_committer = self.pull_request.user.login
            self.last_committer = getattr(self.last_commit.committer, "login", self.parent_committer)
            self.pull_request_branch = self.pull_request.base.ref

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

        self.logger.debug(
            f"\n\n{self.log_prefix} *** END process function, took: {(datetime.now() - self.start_time).seconds} seconds ***\n"
        )

    @property
    def prepare_retest_wellcome_msg(self) -> str:
        retest_msg: str = ""
        if self.tox:
            retest_msg += f" * `/retest {TOX_STR}`: Retest tox\n"

        if self.build_and_push_container:
            retest_msg += f" * `/retest {BUILD_CONTAINER_STR}`: Retest build-container\n"

        if self.pypi:
            retest_msg += f" * `/retest {PYTHON_MODULE_INSTALL_STR}`: Retest python-module-install\n"

        return " * This repository does not support retest actions" if not retest_msg else retest_msg

    def add_api_users_to_auto_verified_and_merged_users(self) -> None:
        apis_and_tokens = get_apis_and_tokes_from_config(config=self.config, repository_name=self.repository_name)
        self.auto_verified_and_merged_users.extend([_api[0].get_user().login for _api in apis_and_tokens])

    def _get_reposiroty_color_for_log_prefix(self) -> str:
        def _get_random_color(_colors: List[str], _json: Dict[str, str]) -> str:
            color = random.choice(_colors)
            _json[self.repository_name] = color

            if _selected := cs(self.repository_name, color).render():
                return _selected

            return self.repository_name

        _all_colors: List[str] = []
        color_json: Dict[str, str]
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

    def prepare_log_prefix(self, pull_request: Optional[PullRequest] = None) -> str:
        _repository_color = self._get_reposiroty_color_for_log_prefix()
        _id = self.x_github_delivery.split("-", 1)[-1]
        return (
            f"{_repository_color}[{self.github_event}][{_id}][PR {pull_request.number}]:"
            if pull_request
            else f"{_repository_color}[{self.github_event}][{_id}]:"
        )

    def process_pull_request_check_run_webhook_data(self) -> None:
        _check_run: Dict[str, Any] = self.hook_data["check_run"]
        check_run_name: str = _check_run["name"]
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
        config_data = self.config.data  # Global repositories configuration
        repo_data = self.config.repository_data(
            repository_name=self.repository_name
        )  # Specific repository configuration

        if not repo_data:
            raise RepositoryNotFoundError(f"Repository {self.repository_name} not found in config file")

        self.repository_full_name: str = repo_data["name"]
        self.github_app_id: str = get_value_from_dicts(
            primary_dict=repo_data, secondary_dict=config_data, key="github-app-id"
        )
        self.pypi: Dict[str, str] = get_value_from_dicts(primary_dict=repo_data, secondary_dict=config_data, key="pypi")
        self.verified_job: bool = get_value_from_dicts(
            primary_dict=repo_data,
            secondary_dict=config_data,
            key="verified-job",
            return_on_none=True,
        )
        self.tox: Dict[str, str] = get_value_from_dicts(primary_dict=repo_data, secondary_dict=config_data, key="tox")
        self.tox_python_version: str = get_value_from_dicts(
            primary_dict=repo_data,
            secondary_dict=config_data,
            key="tox-python-version",
            return_on_none="python",
        )
        self.slack_webhook_url: str = get_value_from_dicts(
            primary_dict=repo_data, secondary_dict=config_data, key="slack_webhook_url"
        )
        self.build_and_push_container: Dict[str, Any] = repo_data.get("container", {})
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
            primary_dict=repo_data,
            secondary_dict=config_data,
            key="pre-commit",
            return_on_none=False,
        )

        self.jira_enabled_repository: bool = False
        self.jira_tracking: bool = get_value_from_dicts(
            primary_dict=repo_data, secondary_dict=config_data, key="jira-tracking"
        )
        self.jira: Dict[str, Any] = get_value_from_dicts(primary_dict=repo_data, secondary_dict=config_data, key="jira")
        if self.jira_tracking and self.jira:
            self.jira_server: str = self.jira["server"]
            self.jira_project: str = self.jira["project"]
            self.jira_token: str = self.jira["token"]
            self.jira_epic: Optional[str] = self.jira.get("epic", "")
            self.jira_user_mapping: Dict[str, str] = self.jira.get("user-mapping", {})
            self.jira_enabled_repository = all([self.jira_server, self.jira_project, self.jira_token])
            if not self.jira_enabled_repository:
                self.logger.error(
                    f"{self.log_prefix} Jira configuration is not valid. Server: {self.jira_server}, "
                    f"Project: {self.jira_project}, Token: {self.jira_token}"
                )

        self.auto_verified_and_merged_users = get_value_from_dicts(
            primary_dict=repo_data,
            secondary_dict=config_data,
            key="auto-verified-and-merged-users",
            return_on_none=[],
        )
        self.can_be_merged_required_labels = get_value_from_dicts(
            primary_dict=repo_data,
            secondary_dict=config_data,
            key="can-be-merged-required-labels",
            return_on_none=[],
        )

    def _get_pull_request(self, number: Optional[int] = None) -> PullRequest:
        if number:
            return self.repository.get_pull(number)

        for _number in extract_key_from_dict(key="number", _dict=self.hook_data):
            try:
                return self.repository.get_pull(_number)
            except GithubException:
                continue

        commit: Dict[str, Any] = self.hook_data.get("commit", {})
        if commit:
            commit_obj = self.repository.get_commit(commit["sha"])
            with contextlib.suppress(Exception):
                return commit_obj.get_pulls()[0]

        raise NoPullRequestError(f"{self.log_prefix} No issue or pull_request found in hook data")

    def _get_last_commit(self) -> Commit:
        return list(self.pull_request.get_commits())[-1]

    def label_exists_in_pull_request(self, label: str) -> bool:
        return any(lb for lb in self.pull_request_labels_names() if lb == label)

    def pull_request_labels_names(self) -> List[str]:
        return [lb.name for lb in self.pull_request.labels] if self.pull_request else []

    def skip_if_pull_request_already_merged(self) -> bool:
        if self.pull_request and self.pull_request.is_merged():
            self.logger.info(f"{self.log_prefix}: PR is merged, not processing")
            return True

        return False

    def _remove_label(self, label: str) -> bool:
        if self.label_exists_in_pull_request(label=label):
            self.logger.info(f"{self.log_prefix} Removing label {label}")
            self.pull_request.remove_from_labels(label)
            return self.wait_for_label(label=label, exists=False)

        self.logger.debug(f"{self.log_prefix} Label {label} not found and cannot be removed")
        return False

    def _add_label(self, label: str) -> None:
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

    def is_branch_exists(self, branch: str) -> Branch:
        return self.repository.get_branch(branch)

    def upload_to_pypi(self, tag_name: str) -> None:
        out: str = ""
        token: str = self.pypi["token"]
        env: str = f"-e TWINE_USERNAME=__token__ -e TWINE_PASSWORD={token} "
        self.logger.info(f"{self.log_prefix} Start uploading to pypi")
        _dist_dir: str = "/tmp/dist"
        cmd: str = (
            f" python3 -m build --sdist --outdir {_dist_dir} ."
            f" && twine check {_dist_dir}/$(echo *.tar.gz)"
            f" && twine upload {_dist_dir}/$(echo *.tar.gz) --skip-existing"
        )
        try:
            rc, out, err = self._run_in_container(command=cmd, env=env, checkout=tag_name)
            if rc:
                self.logger.info(f"{self.log_prefix} Publish to pypi finished")
                if self.slack_webhook_url:
                    message: str = f"""
```
{self.repository_name} Version {tag_name} published to PYPI.
```
"""
                    self.send_slack_message(message=message, webhook_url=self.slack_webhook_url)

        except Exception as exp:
            err = f"Publish to pypi failed: {exp}"
            self.logger.error(f"{self.log_prefix} {err}")
            self.repository.create_issue(
                title=err,
                assignee=self.approvers[0] if self.approvers else "",
                body=f"""
stdout: `{out}`
stderr: `{err}`
""",
            )

    def get_owners_content(self) -> Dict[str, Any]:
        try:
            owners_content: list[ContentFile] | ContentFile = self.repository.get_contents("OWNERS")
            if isinstance(owners_content, list):
                self.logger.debug(f"{self.log_prefix} Found more then one OWNERS file, using the first one")
                owners_content = owners_content[0]

            _content: Dict[str, Any] = yaml.safe_load(owners_content.decoded_content)
            self.logger.debug(f"{self.log_prefix} OWNERS file content: {_content}")
            return _content

        except UnknownObjectException:
            self.logger.error(f"{self.log_prefix} OWNERS file not found")
            return {}

    @property
    def reviewers(self) -> List[str]:
        bc_reviewers: List[str] = self.owners_content.get("reviewers", [])
        if isinstance(bc_reviewers, dict):
            _reviewers: List[str] = self.owners_content.get("reviewers", {}).get("any", [])
        else:
            _reviewers = bc_reviewers

        self.logger.debug(f"{self.log_prefix} Reviewers: {_reviewers}")
        return _reviewers

    @property
    def files_reviewers(self) -> Dict[str, str]:
        _reviewers = self.owners_content.get("reviewers", {})
        if isinstance(_reviewers, dict):
            return _reviewers.get("files", {})

        return {}

    @property
    def folders_reviewers(self) -> Dict[str, str]:
        _reviewers = self.owners_content.get("reviewers", {})
        if isinstance(_reviewers, dict):
            return _reviewers.get("folders", {})

        return {}

    @property
    def approvers(self) -> List[str]:
        return self.owners_content.get("approvers", [])

    def list_changed_commit_files(self) -> list[str]:
        return [fd["filename"] for fd in self.last_commit.raw_data["files"]]

    def assign_reviewers(self) -> None:
        self.logger.info(f"{self.log_prefix} Assign reviewers")
        changed_files = self.list_changed_commit_files()
        reviewers_to_add = self.reviewers
        for _file, _file_reviewers in self.files_reviewers.items():
            if _file in changed_files:
                reviewers_to_add.extend(_file_reviewers)

        for _folder, _folder_reviewers in self.folders_reviewers.items():
            if any(cf for cf in changed_files if _folder in str(Path(cf).parent)):
                reviewers_to_add.extend(_folder_reviewers)

        _to_add: List[str] = list(set(reviewers_to_add))
        self.logger.debug(f"{self.log_prefix} Reviewers to add: {', '.join(_to_add)}")
        for reviewer in _to_add:
            if reviewer != self.pull_request.user.login:
                self.logger.debug(f"{self.log_prefix} Adding reviewer {reviewer}")
                try:
                    self.pull_request.create_review_request([reviewer])
                except GithubException as ex:
                    self.logger.debug(f"{self.log_prefix} Failed to add reviewer {reviewer}. {ex}")
                    self.pull_request.create_issue_comment(f"{reviewer} can not be added as reviewer. {ex}")

    def add_size_label(self) -> None:
        size: int = self.pull_request.additions + self.pull_request.deletions
        if size < 20:
            _label = "XS"

        elif size < 50:
            _label = "S"

        elif size < 100:
            _label = "M"

        elif size < 300:
            _label = "L"

        elif size < 500:
            _label = "XL"

        else:
            _label = "XXL"

        size_label = f"{SIZE_LABEL_PREFIX}{_label}"

        if size_label in self.pull_request_labels_names():
            return

        exists_size_label = [label for label in self.pull_request_labels_names() if label.startswith(SIZE_LABEL_PREFIX)]

        if exists_size_label:
            self._remove_label(label=exists_size_label[0])

        self._add_label(label=size_label)

    def label_by_user_comment(
        self, user_requested_label: str, remove: bool, reviewed_user: str, issue_comment_id: int
    ) -> None:
        self.logger.debug(
            f"{self.log_prefix} {DELETE_STR if remove else ADD_STR} "
            f"label requested by user {reviewed_user}: {user_requested_label}"
        )
        self.create_comment_reaction(issue_comment_id=issue_comment_id, reaction=REACTIONS.ok)

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

    def set_run_tox_check_failure(self, output: Dict[str, Any]) -> None:
        return self.set_check_run_status(check_run=TOX_STR, conclusion=FAILURE_STR, output=output)

    def set_run_tox_check_success(self, output: Dict[str, Any]) -> None:
        return self.set_check_run_status(check_run=TOX_STR, conclusion=SUCCESS_STR, output=output)

    def set_run_pre_commit_check_queued(self) -> None:
        if not self.pre_commit:
            return

        return self.set_check_run_status(check_run=PRE_COMMIT_STR, status=QUEUED_STR)

    def set_run_pre_commit_check_in_progress(self) -> None:
        return self.set_check_run_status(check_run=PRE_COMMIT_STR, status=IN_PROGRESS_STR)

    def set_run_pre_commit_check_failure(self, output: Optional[Dict[str, Any]] = None) -> None:
        return self.set_check_run_status(check_run=PRE_COMMIT_STR, conclusion=FAILURE_STR, output=output)

    def set_run_pre_commit_check_success(self, output: Optional[Dict[str, Any]] = None) -> None:
        return self.set_check_run_status(check_run=PRE_COMMIT_STR, conclusion=SUCCESS_STR, output=output)

    def set_merge_check_queued(self, output: Optional[Dict[str, Any]] = None) -> None:
        return self.set_check_run_status(check_run=CAN_BE_MERGED_STR, status=QUEUED_STR, output=output)

    def set_merge_check_in_progress(self) -> None:
        return self.set_check_run_status(check_run=CAN_BE_MERGED_STR, status=IN_PROGRESS_STR)

    def set_merge_check_success(self) -> None:
        return self.set_check_run_status(check_run=CAN_BE_MERGED_STR, conclusion=SUCCESS_STR)

    def set_merge_check_failure(self, output: Dict[str, Any]) -> None:
        return self.set_check_run_status(check_run=CAN_BE_MERGED_STR, conclusion=FAILURE_STR, output=output)

    def set_container_build_queued(self) -> None:
        if not self.build_and_push_container:
            return

        return self.set_check_run_status(check_run=BUILD_CONTAINER_STR, status=QUEUED_STR)

    def set_container_build_in_progress(self) -> None:
        return self.set_check_run_status(check_run=BUILD_CONTAINER_STR, status=IN_PROGRESS_STR)

    def set_container_build_success(self, output: Dict[str, Any]) -> None:
        return self.set_check_run_status(check_run=BUILD_CONTAINER_STR, conclusion=SUCCESS_STR, output=output)

    def set_container_build_failure(self, output: Dict[str, Any]) -> None:
        return self.set_check_run_status(check_run=BUILD_CONTAINER_STR, conclusion=FAILURE_STR, output=output)

    def set_python_module_install_queued(self) -> None:
        if not self.pypi:
            return

        return self.set_check_run_status(check_run=PYTHON_MODULE_INSTALL_STR, status=QUEUED_STR)

    def set_python_module_install_in_progress(self) -> None:
        return self.set_check_run_status(check_run=PYTHON_MODULE_INSTALL_STR, status=IN_PROGRESS_STR)

    def set_python_module_install_success(self, output: Dict[str, Any]) -> None:
        return self.set_check_run_status(check_run=PYTHON_MODULE_INSTALL_STR, conclusion=SUCCESS_STR, output=output)

    def set_python_module_install_failure(self, output: Dict[str, Any]) -> None:
        return self.set_check_run_status(check_run=PYTHON_MODULE_INSTALL_STR, conclusion=FAILURE_STR, output=output)

    def set_cherry_pick_in_progress(self) -> None:
        return self.set_check_run_status(check_run=CHERRY_PICKED_LABEL_PREFIX, status=IN_PROGRESS_STR)

    def set_cherry_pick_success(self, output: Dict[str, Any]) -> None:
        return self.set_check_run_status(check_run=CHERRY_PICKED_LABEL_PREFIX, conclusion=SUCCESS_STR, output=output)

    def set_cherry_pick_failure(self, output: Dict[str, Any]) -> None:
        return self.set_check_run_status(check_run=CHERRY_PICKED_LABEL_PREFIX, conclusion=FAILURE_STR, output=output)

    def create_issue_for_new_pull_request(self) -> None:
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
        base_regctl_command = (
            "podman run --rm --net host  -v regctl-conf:/home/appuser/.regctl/ ghcr.io/regclient/regctl:latest"
        )
        registry_info = self.container_repository.split("/")
        registry_url = "" if len(registry_info) < 3 else registry_info[0]

        rc, out, err = run_command(
            command=f"{base_regctl_command} registry login {registry_url} -u {self.container_repository_username} "
            f"-p {self.container_repository_password}",
            log_prefix=self.log_prefix,
        )
        if rc:
            rc, out, err = run_command(
                command=f"{base_regctl_command} tag ls {self.container_repository} --include {pr_tag}",
                log_prefix=self.log_prefix,
            )
            if rc and out:
                if run_command(
                    command=f"{base_regctl_command} tag delete {repository_full_tag}",
                    log_prefix=self.log_prefix,
                )[0]:
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
        if comment_action := self.hook_data["action"] in ("action", "deleted"):
            self.logger.debug(f"{self.log_prefix} Not processing comment. action is {comment_action}")
            return

        self.logger.info(f"{self.log_prefix} Processing issue {self.hook_data['issue']['number']}")

        body: str = self.hook_data["comment"]["body"]

        if body == self.welcome_msg:
            self.logger.debug(
                f"{self.log_prefix} Welcome message found in issue {self.pull_request.title}. Not processing"
            )
            return

        _user_commands: List[str] = [_cmd.strip("/") for _cmd in body.strip().splitlines() if _cmd.startswith("/")]

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

        pull_request_data: Dict[str, Any] = self.hook_data["pull_request"]
        self.parent_committer = pull_request_data["user"]["login"]
        self.pull_request_branch = pull_request_data["base"]["ref"]

        if hook_action == "edited":
            self.set_wip_label_based_on_title()

        if hook_action == "opened":
            self.logger.info(f"{self.log_prefix} Creating welcome comment")
            self.pull_request.create_issue_comment(self.welcome_msg)
            self.create_issue_for_new_pull_request()
            self.process_opened_or_synchronize_pull_request()
            self.set_wip_label_based_on_title()

            if self.jira_track_pr:
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
                        self.logger.warning(
                            f"{self.log_prefix} Jira epic or assignee is not set. Skipping Jira story creation"
                        )

        if hook_action == "synchronize":
            for _label in self.pull_request.labels:
                _label_name = _label.name
                if (
                    _label_name.startswith(APPROVED_BY_LABEL_PREFIX)
                    or _label_name.startswith(COMMENTED_BY_LABEL_PREFIX)
                    or _label_name.startswith(CHANGED_REQUESTED_BY_LABEL_PREFIX)
                    or _label_name.startswith(LGTM_BY_LABEL_PREFIX)
                ):
                    self._remove_label(label=_label_name)

            self.process_opened_or_synchronize_pull_request()

            if self.jira_track_pr:
                jira_conn = self.get_jira_conn()
                if not jira_conn:
                    self.logger.error(f"{self.log_prefix} Jira connection not found")

                else:
                    if _story_key := self.get_story_key_with_jira_connection():
                        if not self.jira_assignee:
                            self.logger.warning(
                                f"{self.log_prefix} Jira assignee is not set. Skipping sub-task creation"
                            )

                        else:
                            self.logger.info(f"{self.log_prefix} Creating sub-task for Jira story {_story_key}")
                            jira_conn.create_closed_subtask(
                                title=f"{self.issue_title}: New commit from {self.last_committer}",
                                parent_key=_story_key,
                                assignee=self.jira_assignee,
                                body=f"PR: {self.pull_request.title}, new commit pushed by {self.last_committer}",
                            )

        if hook_action == "closed":
            self.close_issue_for_merged_or_closed_pr(hook_action=hook_action)
            self.delete_remote_tag_for_merged_or_closed_pr()
            is_merged = pull_request_data.get("merged")

            if is_merged:
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
                self.label_by_pull_requests_merge_state_after_merged()
                self.pull_request = original_pull_request

            if self.jira_track_pr:
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

        if hook_action in ("labeled", "unlabeled"):
            _check_for_merge: bool = False
            _reviewer: Optional[str] = None
            action_labeled = hook_action == "labeled"
            labeled = self.hook_data["label"]["name"].lower()
            if labeled == CAN_BE_MERGED_STR:
                return

            self.logger.info(f"{self.log_prefix} PR {self.pull_request.number} {hook_action} with {labeled}")
            if labeled.startswith(APPROVED_BY_LABEL_PREFIX):
                _reviewer = labeled.split(APPROVED_BY_LABEL_PREFIX)[-1]

            if labeled.startswith(CHANGED_REQUESTED_BY_LABEL_PREFIX):
                _reviewer = labeled.split(CHANGED_REQUESTED_BY_LABEL_PREFIX)[-1]

            if _reviewer in self.approvers:
                _check_for_merge = True

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

    def manage_reviewed_by_label(self, review_state: str, action: str, reviewed_user: str) -> None:
        self.logger.info(
            f"{self.log_prefix} "
            f"Processing label for review from {reviewed_user}. "
            f"review_state: {review_state}, action: {action}"
        )
        label_prefix: str = ""
        label_to_remove: str = ""

        if reviewed_user in self.approvers:
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

        cmd = f"{self.tox_python_version} -m {TOX_STR}"
        _tox_tests = self.tox.get(self.pull_request_branch, "")
        if _tox_tests != "all":
            tests = _tox_tests.replace(" ", "")
            cmd += f" -e {tests}"

        self.set_run_tox_check_in_progress()
        rc, out, err = self._run_in_container(command=cmd)

        output: Dict[str, Any] = {
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

        cmd = f"{PRE_COMMIT_STR} run --all-files"
        self.set_run_pre_commit_check_in_progress()
        rc, out, err = self._run_in_container(command=cmd)

        output: Dict[str, Any] = {
            "title": "Pre-Commit",
            "summary": "",
            "text": self.get_check_run_text(err=err, out=out),
        }
        if rc:
            return self.set_run_pre_commit_check_success(output=output)
        else:
            return self.set_run_pre_commit_check_failure(output=output)

    def user_commands(self, command: str, reviewed_user: str, issue_comment_id: int) -> None:
        available_commands: List[str] = [
            COMMAND_RETEST_STR,
            COMMAND_CHERRY_PICK_STR,
            COMMAND_ASSIGN_REVIEWERS_STR,
            COMMAND_CHECK_CAN_MERGE_STR,
            BUILD_AND_PUSH_CONTAINER_STR,
        ]

        # command_required_args: List[str] = [COMMAND_RETEST_STR]
        # skip_msg: str = f"Pull request already merged, not running {command}"

        command_and_args: List[str] = command.split(" ", 1)
        _command = command_and_args[0]
        _args: str = command_and_args[1] if len(command_and_args) > 1 else ""

        self.logger.debug(
            f"{self.log_prefix} User: {reviewed_user}, Command: {_command}, Command args: {_args if _args else 'None'}"
        )
        if _command not in available_commands + list(USER_LABELS_DICT.keys()):
            self.logger.debug(f"{self.log_prefix} Command {command} is not supported.")
            return

        # if _command == COMMAND_CHERRY_PICK_STR:
        #     if self.skip_if_pull_request_already_merged():
        #         self.logger.debug(f"{self.log_prefix} {skip_msg")
        #         self.pull_request.create_issue_comment(skip_msg)
        #         return

        self.logger.info(f"{self.log_prefix} Processing label/user command {command} by user {reviewed_user}")

        if remove := len(command_and_args) > 1 and _args == "cancel":
            self.logger.debug(f"{self.log_prefix} User requested 'cancel' for command {_command}")

        if _command == COMMAND_RETEST_STR and not _args:
            comment_msg: str = f"{_command} requires an argument"
            error_msg: str = f"{self.log_prefix} {comment_msg}"
            self.logger.debug(error_msg)
            self.pull_request.create_issue_comment(comment_msg)

        elif _command == COMMAND_ASSIGN_REVIEWERS_STR:
            self.assign_reviewers()

        if _command == COMMAND_CHECK_CAN_MERGE_STR:
            self.check_if_can_be_merged()

        if _command == COMMAND_CHERRY_PICK_STR:
            self.process_cherry_pick_command(
                issue_comment_id=issue_comment_id, command_args=_args, reviewed_user=reviewed_user
            )

        elif _command == COMMAND_RETEST_STR:
            self.process_retest_command(issue_comment_id=issue_comment_id, command_args=_args)

        elif _command == BUILD_AND_PUSH_CONTAINER_STR:
            if self.build_and_push_container:
                self.create_comment_reaction(issue_comment_id=issue_comment_id, reaction=REACTIONS.ok)
                self._run_build_container(push=True, set_check=False, command_args=_args)
            else:
                msg = f"No {BUILD_AND_PUSH_CONTAINER_STR} configured for this repository"
                error_msg = f"{self.log_prefix} {msg}"
                self.logger.debug(error_msg)
                self.pull_request.create_issue_comment(msg)

        elif _command == WIP_STR:
            self.create_comment_reaction(issue_comment_id=issue_comment_id, reaction=REACTIONS.ok)
            wip_for_title: str = f"{WIP_STR.upper()}:"
            if remove:
                self._remove_label(label=WIP_STR)
                self.pull_request.edit(title=self.pull_request.title.replace(wip_for_title, ""))
            else:
                self._add_label(label=WIP_STR)
                self.pull_request.edit(title=f"{wip_for_title} {self.pull_request.title}")

        else:
            self.label_by_user_comment(
                user_requested_label=_command,
                remove=remove,
                reviewed_user=reviewed_user,
                issue_comment_id=issue_comment_id,
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
            env = f"-e GITHUB_TOKEN={self.token}"
            cmd = (
                f" git checkout {target_branch}"
                f" && git pull origin {target_branch}"
                f" && git checkout -b {new_branch_name} origin/{target_branch}"
                f" && git cherry-pick {commit_hash}"
                f" && git push origin {new_branch_name}"
                f" && hub pull-request "
                f"-b {target_branch} "
                f"-h {new_branch_name} "
                f"-l {CHERRY_PICKED_LABEL_PREFIX} "
                f'-m "{CHERRY_PICKED_LABEL_PREFIX}: [{target_branch}] {commit_msg_striped}" '
                f'-m "cherry-pick {pull_request_url} into {target_branch}" '
                f'-m "requested-by {requested_by}"'
            )
            rc, out, err = self._run_in_container(command=cmd, env=env)

            output = {
                "title": "Cherry-pick details",
                "summary": "",
                "text": self.get_check_run_text(err=err, out=out),
            }
            if rc:
                self.set_cherry_pick_success(output=output)
                self.pull_request.create_issue_comment(
                    f"Cherry-picked PR {self.pull_request.title} into {target_branch}"
                )
            else:
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

    def label_by_pull_requests_merge_state_after_merged(self) -> None:
        """
        Labels pull requests based on their mergeable state.

        If the mergeable state is 'behind', the 'needs rebase' label is added.
        If the mergeable state is 'dirty', the 'has conflicts' label is added.
        """
        time_sleep = 30
        self.logger.info(f"{self.log_prefix} Sleep for {time_sleep} seconds before getting all opened PRs")
        time.sleep(time_sleep)

        for pull_request in self.repository.get_pulls(state="open"):
            self.pull_request = pull_request
            self.logger.info(f"{self.log_prefix} check label pull request after merge")
            self.label_pull_request_by_merge_state(_sleep=time_sleep)

    def label_pull_request_by_merge_state(self, _sleep: int = 0) -> None:
        if _sleep:
            self.logger.info(f"{self.log_prefix} Sleep for {_sleep} seconds before checking merge state")
            time.sleep(_sleep)

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
            self.all_required_status_checks = self.get_all_required_status_checks()
            self.logger.info(f"{self.log_prefix} Check if {CAN_BE_MERGED_STR}.")
            self.set_merge_check_queued()
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
                failure_output += f"Some required check runs in progress {', '.join(check_runs_in_progress)}\n"

            _labels = self.pull_request_labels_names()
            is_hold = HOLD_LABEL_STR in _labels
            is_wip = WIP_STR in _labels
            if is_hold or is_wip:
                if is_hold:
                    failure_output += "Hold label exists.\n"

                if is_wip:
                    failure_output += "WIP label exists.\n"

            if not self.pull_request.mergeable:
                failure_output += "PR is not mergeable: {self.pull_request.mergeable_state}\n"

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
                failure_output += f"Some check runs failed: {', '.join(exclude_in_progress)}\n"

            self.logger.debug(f"{self.log_prefix} check if can be merged. PR labels are: {_labels}")

            for _label in _labels:
                if CHANGED_REQUESTED_BY_LABEL_PREFIX.lower() in _label.lower():
                    change_request_user = _label.split("-")[-1]
                    if change_request_user in self.approvers:
                        failure_output += "PR has changed requests from approvers\n"

            missing_required_labels = []
            for _req_label in self.can_be_merged_required_labels:
                if _req_label not in _labels:
                    missing_required_labels.append(_req_label)

            if missing_required_labels:
                failure_output += f"Missing required labels: {', '.join(missing_required_labels)}\n"

            pr_approved = False
            for _label in _labels:
                if APPROVED_BY_LABEL_PREFIX.lower() in _label.lower():
                    approved_user = _label.split("-")[-1]
                    if approved_user in self.approvers and self.parent_committer != approved_user:
                        pr_approved = True
                        break

            if not pr_approved:
                missing_approvers = [approver for approver in self.approvers if approver != self.parent_committer]
                failure_output += f"Missing lgtm/approved from approvers: {', '.join(missing_approvers)}\n"

            if pr_approved and not failure_output:
                self._add_label(label=CAN_BE_MERGED_STR)
                self.set_merge_check_success()
                if self.parent_committer in self.auto_verified_and_merged_users:
                    self.logger.info(
                        f"{self.log_prefix} will be merged automatically. owner: {self.parent_committer} "
                        f"is part of {self.auto_verified_and_merged_users}"
                    )
                    self.pull_request.create_issue_comment(
                        f"Owner of the pull request {self.parent_committer} "
                        f"is part of:\n`{self.auto_verified_and_merged_users}`\n"
                        "Pull request is merged automatically."
                    )
                    self.pull_request.merge(merge_method="squash")

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

        pull_request = hasattr(self, "pull_request")

        if pull_request and set_check:
            if self.is_check_run_in_progress(check_run=BUILD_CONTAINER_STR) and not is_merged:
                self.logger.info(f"{self.log_prefix} Check run is in progress, not running {BUILD_CONTAINER_STR}.")
                return

            self.set_container_build_in_progress()

        _container_repository_and_tag = self._container_repository_and_tag(is_merged=is_merged, tag=tag)
        no_cache: str = " --no-cache" if is_merged else ""
        build_cmd: str = f"--network=host {no_cache} -f {self.container_repo_dir}/{self.dockerfile} . -t {_container_repository_and_tag}"

        if self.container_build_args:
            build_args: str = [f"--build-arg {b_arg}" for b_arg in self.container_build_args][0]
            build_cmd = f"{build_args} {build_cmd}"

        if self.container_command_args:
            build_cmd = f"{' '.join(self.container_command_args)} {build_cmd}"

        if command_args:
            build_cmd = f"{command_args} {build_cmd}"

        if push:
            repository_creds: str = f"{self.container_repository_username}:{self.container_repository_password}"
            build_cmd += f" && podman push --creds {repository_creds} {_container_repository_and_tag}"
        podman_build_cmd: str = f"podman build {build_cmd}"

        rc, out, err = self._run_in_container(command=podman_build_cmd, is_merged=is_merged, tag_name=tag)
        output: Dict[str, str] = {
            "title": "Build container",
            "summary": "",
            "text": self.get_check_run_text(err=err, out=out),
        }
        if rc:
            self.logger.info(f"{self.log_prefix} Done building {_container_repository_and_tag}")
            if pull_request and set_check:
                return self.set_container_build_success(output=output)

            if push:
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
            if push:
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

            if self.pull_request and set_check:
                return self.set_container_build_failure(output=output)

    def _run_install_python_module(self) -> None:
        if not self.pypi:
            return

        if self.is_check_run_in_progress(check_run=PYTHON_MODULE_INSTALL_STR):
            self.logger.info(f"{self.log_prefix} Check run is in progress, not running {PYTHON_MODULE_INSTALL_STR}.")
            return

        self.logger.info(f"{self.log_prefix} Installing python module")
        f"{PYTHON_MODULE_INSTALL_STR}-{shortuuid.uuid()}"
        self.set_python_module_install_in_progress()
        rc, out, err = self._run_in_container(command="pip install .")
        output: Dict[str, str] = {
            "title": "Python module installation",
            "summary": "",
            "text": self.get_check_run_text(err=err, out=out),
        }
        if rc:
            return self.set_python_module_install_success(output=output)

        return self.set_python_module_install_failure(output=output)

    def send_slack_message(self, message: str, webhook_url: str) -> None:
        slack_data: Dict[str, str] = {"text": message}
        self.logger.info(f"{self.log_prefix} Sending message to slack: {message}")
        response: requests.Response = requests.post(
            webhook_url,
            data=json.dumps(slack_data),
            headers={"Content-Type": "application/json"},
        )
        if response.status_code != 200:
            raise ValueError(
                f"Request to slack returned an error {response.status_code} with the following message: "
                f"{response.text}"
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
        prepare_pull_futures: List[Future] = []
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

        run_check_runs_futures: List[Future] = []
        with ThreadPoolExecutor() as executor:
            run_check_runs_futures.append(executor.submit(self._run_tox))
            run_check_runs_futures.append(executor.submit(self._run_pre_commit))
            run_check_runs_futures.append(executor.submit(self._run_install_python_module))
            run_check_runs_futures.append(executor.submit(self._run_build_container))

        for result in as_completed(prepare_pull_futures):
            if _exp := result.exception():
                self.logger.error(f"{self.log_prefix} {_exp}")

        for result in as_completed(run_check_runs_futures):
            if _exp := result.exception():
                self.logger.error(f"{self.log_prefix} {_exp}")

            self.logger.info(f"{self.log_prefix} {result.result()}")

        try:
            self.logger.info(f"{self.log_prefix} Adding PR owner as assignee")
            self.pull_request.add_to_assignees()
        except Exception:
            if self.approvers:
                self.pull_request.add_to_assignees(self.approvers[0])

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
        output: Optional[Dict[str, str]] = None,
    ) -> None:
        kwargs: Dict[str, Any] = {"name": check_run, "head_sha": self.last_commit.sha}

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

    def _run_in_container(
        self,
        command: str,
        env: str = "",
        is_merged: bool = False,
        checkout: str = "",
        tag_name: str = "",
    ) -> Tuple[int, str, str]:
        podman_base_cmd: str = (
            "podman run --network=host --privileged -v /tmp/containers:/var/lib/containers/:Z "
            f"--rm {env if env else ''} --entrypoint bash quay.io/myakove/github-webhook-server -c"
        )

        # Clone the repository
        clone_base_cmd: str = (
            f"git clone {self.repository.clone_url.replace('https://', f'https://{self.token}@')} "
            f"{self.container_repo_dir}"
        )
        clone_base_cmd += f" && cd {self.container_repo_dir}"
        clone_base_cmd += f" && git config user.name '{self.repository.owner.login}'"
        clone_base_cmd += f" && git config user.email '{self.repository.owner.email}'"
        clone_base_cmd += " && git config --local --add remote.origin.fetch +refs/pull/*/head:refs/remotes/origin/pr/*"
        clone_base_cmd += " && git remote update >/dev/null 2>&1"

        # Checkout to requested branch/tag
        if checkout:
            clone_base_cmd += f" && git checkout {checkout}"

        # Checkout the branch if pull request is merged or for release
        else:
            if is_merged:
                clone_base_cmd += f" && git checkout {self.pull_request_branch}"

            elif tag_name:
                clone_base_cmd += f" && git checkout {tag_name}"

            # Checkout the pull request
            else:
                try:
                    pull_request = self._get_pull_request()
                except NoPullRequestError:
                    self.logger.error(f"{self.log_prefix} [func:_run_in_container] No pull request found")
                    return False, "", ""

                clone_base_cmd += f" && git checkout origin/pr/{pull_request.number}"

        # final podman command
        podman_base_cmd += f" '{clone_base_cmd} && {command}'"
        return run_command(command=podman_base_cmd, log_prefix=self.log_prefix)

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

    #     def log_repository_features(self):
    #         repository_features = f"""
    #                         auto-verified-and-merged-users: {self.auto_verified_and_merged_users}
    #                         can-be-merged-required-labels: {self.can_be_merged_required_labels}
    #                         pypi: {self.pypi}
    #                         verified-job: {self.verified_job}
    #                         tox-enabled: {self.tox_enabled}
    #                         tox-python-version: {self.tox_python_version}
    #                         pre-commit: {self.pre_commit}
    #                         slack-webhook-url: {self.slack_webhook_url}
    #                         container: {self.build_and_push_container}
    #                         jira-tracking: {self.jira_tracking}
    #                         jira-server: {self.jira_server}
    #                         jira-project: {self.jira_project}
    #                         jira-token: {self.jira_token}
    #                         jira-enabled-repository: {self.jira_enabled_repository}
    #                         jira-user-mapping: {self.jira_user_mapping}
    # """
    #         self.logger.info(f"{self.log_prefix} Repository features: {repository_features}")

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

    def get_branch_required_status_checks(self) -> List[str]:
        if self.repository.private:
            self.logger.info(
                f"{self.log_prefix} Repository is private, skipping getting branch protection required status checks"
            )
            return []

        pull_request_branch = self.repository.get_branch(self.pull_request_branch)
        branch_protection = pull_request_branch.get_protection()
        return branch_protection.required_status_checks.contexts

    def get_all_required_status_checks(self) -> List[str]:
        if not hasattr(self, "pull_request_branch"):
            self.pull_request_branch = self.pull_request.base.ref

        all_required_status_checks: List[str] = []
        branch_required_status_checks = self.get_branch_required_status_checks()
        if self.tox:
            all_required_status_checks.append(TOX_STR)

        if self.verified_job:
            all_required_status_checks.append(VERIFIED_LABEL_STR)

        if self.build_and_push_container:
            all_required_status_checks.append(BUILD_CONTAINER_STR)

        if self.pypi:
            all_required_status_checks.append(PYTHON_MODULE_INSTALL_STR)

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
            reviewers_and_approvers = self.reviewers + self.approvers
            if self.parent_committer in reviewers_and_approvers:
                self.jira_assignee = self.jira_user_mapping.get(self.parent_committer)
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

    def process_cherry_pick_command(self, issue_comment_id: int, command_args: str, reviewed_user: str) -> None:
        self.create_comment_reaction(issue_comment_id=issue_comment_id, reaction=REACTIONS.ok)
        _target_branches: List[str] = command_args.split()
        _exits_target_branches: Set[str] = set()
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
                cp_labels: List[str] = [
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

    def process_retest_command(self, issue_comment_id: int, command_args: str) -> None:
        _target_tests: List[str] = command_args.split()
        for _test in _target_tests:
            if _test == TOX_STR:
                if not self.tox:
                    msg: str = f"No {TOX_STR} configured for this repository"
                    error_msg = f"{self.log_prefix} {msg}."
                    self.logger.info(error_msg)
                    self.pull_request.create_issue_comment(msg)
                    return

                self.create_comment_reaction(issue_comment_id=issue_comment_id, reaction=REACTIONS.ok)
                self._run_tox()

            elif _test == PRE_COMMIT_STR:
                self.create_comment_reaction(issue_comment_id=issue_comment_id, reaction=REACTIONS.ok)
                self._run_pre_commit()

            elif _test == BUILD_CONTAINER_STR:
                if self.build_and_push_container:
                    self.create_comment_reaction(issue_comment_id=issue_comment_id, reaction=REACTIONS.ok)
                    self._run_build_container()
                else:
                    msg = f"No {BUILD_CONTAINER_STR} configured for this repository"
                    error_msg = f"{self.log_prefix} {msg}"
                    self.logger.info(error_msg)
                    self.pull_request.create_issue_comment(msg)

            elif _test == PYTHON_MODULE_INSTALL_STR:
                if not self.pypi:
                    error_msg = f"{self.log_prefix} No pypi configured"
                    self.logger.info(error_msg)
                    self.pull_request.create_issue_comment(error_msg)
                    return

                self.create_comment_reaction(issue_comment_id=issue_comment_id, reaction=REACTIONS.ok)
                self._run_install_python_module()
