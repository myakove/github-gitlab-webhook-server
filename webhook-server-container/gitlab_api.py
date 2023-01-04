import os
import re

import gitlab
import requests
import yaml
from constants import ADD_STR, DELETE_STR, DYNAMIC_LABELS_DICT, USER_LABELS_DICT
from gitlab.exceptions import GitlabUpdateError


class GitLabApi:
    def __init__(self, app, hook_data):
        self.app = app
        self.internal_api = self.get_internal_api()
        self.hook_data = hook_data
        self.obj_kind = self.hook_data["object_kind"]
        self.repository = self.internal_api.projects.get(
            self.hook_data["project"]["id"]
        )
        self.repository_full_name = self.hook_data["repository"]["name"]
        self.base_url = self.hook_data["repository"]["homepage"]
        self.merge_request = self.get_mr_data()
        self.verified_label = "verified"
        self.lgtm_label = "lgtm"
        self.label_by_str = "-By-"
        self.can_be_merged_label = "can-be-merged"
        self.repo_mr_log_message = (
            f"{self.repository_full_name} {self.merge_request.iid}:"
        )
        self.user = self.hook_data["user"]
        self.username = self.user["username"]
        supported_user_labels_str = "".join(
            [f"    * {label}\n" for label in USER_LABELS_DICT.keys()]
        )
        self.welcome_msg = f"""
** AUTOMATED **
This is automated comment.

The following are automatically added:
 * Mention reviewers from OWNER file (in the root of the repository).
 * MR assignee
 * `Approved-by-<username>` or `Reviewed-by-<username>` label when an approve comment is added (see below).
 * `verified` label when verified comment is added (see below).

Available user actions:
 * To mark MR as verified add `!verified` to a PR comment, to un-verify add `!-verified` to a MR comment.
        Verified label removed on each new commit push.
 * To approve an MR, either use the `Approve` button or add `!LGTM` or `!lgtm` to the MR comment.
 * To remove approval, either use the `Revoke approval` button or add `!-LGTM` or `!-lgtm` to the MR comment.
  * To add a label by comment use `!<label name>`, to remove, use `!-<label name>`
        Supported labels:
            {supported_user_labels_str}
            """

        # Always make sure that the repository's merge requests "All threads must be resolved" setting is enabled
        if not self.repository.only_allow_merge_if_all_discussions_are_resolved:
            self.repository.manager.update(
                self.repository.id,
                {"only_allow_merge_if_all_discussions_are_resolved": True},
            )

    def process_hook(self, data):
        try:
            event_type = data["event_type"]
            if event_type == "merge_request":
                action = data["object_attributes"]["action"]
                if action == "open":
                    self.process_new_merge_request_webhook_data()
                if action == "update":
                    self.process_updated_merge_request_webhook_data()
                if action == "approved":
                    self.process_approved_merge_request_webhook_data()
                if action == "unapproved":
                    self.process_unapproved_merge_request_webhook_data()

            if event_type == "note":
                self.process_comment_webhook_data()

        except Exception as ex:
            self.app.logger.error(f"Exception {ex}\n{data}")

    @staticmethod
    def get_internal_api():
        container_gitlab_config = "/python-gitlab/python-gitlab.cfg"
        if os.path.isfile(container_gitlab_config):
            config_files = [container_gitlab_config]
        else:
            config_files = [os.path.join(os.path.expanduser("~"), "python-gitlab.cfg")]
        gitlab_api = gitlab.Gitlab.from_config(config_files=config_files)
        gitlab_api.auth()
        return gitlab_api

    def get_mr_data(self):
        if self.obj_kind == "merge_request":
            mr_id = self.hook_data["object_attributes"]["iid"]
        else:
            mr_id = self.hook_data["merge_request"]["iid"]
        self.app.logger.info(
            f"{mr_id} {self.obj_kind}: Processing... {self.hook_data['project']['name']}"
        )
        return self.repository.mergerequests.get(mr_id)

    @property
    def owners_dict(self):
        owners_file_raw_url = f"{self.base_url}/-/raw/main/OWNERS"
        resp = requests.get(owners_file_raw_url, verify=False)
        if resp.status_code != requests.codes.ok:
            return {}
        return yaml.safe_load(resp.text)

    @property
    def reviewers(self):
        return self.owners_dict.get("reviewers", [])

    @property
    def approvers(self):
        return self.owners_dict.get("approvers", [])

    def label_by_user_comment(self, user_request):
        _label = user_request[1]
        if not any(_label.lower() in label_name for label_name in USER_LABELS_DICT):
            self.app.logger.info(
                f"Label {_label} is not a predefined one, will not be added / removed."
            )
            return

        # Remove label
        if user_request[0] == "-":
            self.app.logger.info(
                f"{self.repo_mr_log_message} Label removal requested by user: {_label}"
            )
            if self.lgtm_label in _label.lower():
                if self.approved_by_label in self.merge_request.labels:
                    self.add_remove_user_approve_label(action=DELETE_STR)

            else:
                self.update_merge_request(attribute_dict={"remove_labels": [_label]})
        # Add label
        else:
            self.app.logger.info(
                f"{self.repo_mr_log_message} Label addition requested by user: {_label}"
            )
            if self.lgtm_label in _label.lower():
                if self.approved_by_label not in self.merge_request.labels:
                    self.add_remove_user_approve_label(action=ADD_STR)
            else:
                self.update_merge_request(attribute_dict={"add_labels": [_label]})

    def reset_verify_label(self):
        self.app.logger.info(
            f"{self.repo_mr_log_message}: Processing reset verify label on new commit push"
        )
        # Remove Verified label
        if self.verified_label in self.merge_request.labels:
            self.app.logger.info(f"{self.repo_mr_log_message} Removing verified label.")
            self.update_merge_request(
                attribute_dict={"remove_labels": [self.verified_label]}
            )

    def add_welcome_message(self):
        self.app.logger.info(f"{self.repo_mr_log_message} Creating welcome comment")
        self.merge_request.notes.create({"body": self.welcome_msg})

    def process_comment_webhook_data(self):
        note_body = self.hook_data["object_attributes"]["description"]
        if note_body == self.welcome_msg.rstrip():
            self.app.logger.info(
                f"{self.repo_mr_log_message} Welcome message found in comment; skipping comment processing"
            )
            return
        user_requests = re.findall(r"!(-)?(.*)", note_body)
        if user_requests:
            self.app.logger.info(f"{self.repo_mr_log_message} Note body: {note_body}")
            for user_request in user_requests:
                self.app.logger.info(
                    f"{self.repo_mr_log_message} Processing label by user comment"
                )
                self.label_by_user_comment(user_request=user_request)

    def process_new_merge_request_webhook_data(self):
        # TODO: create new issue, set_label_size
        self.add_welcome_message()
        self.add_assignee()
        self.add_reviewers()

    def process_updated_merge_request_webhook_data(self):
        # TODO: Replace with bot actions
        if self.hook_data["changes"].get("labels"):
            if self.can_be_merged():
                if self.can_be_merged_label not in self.merge_request.labels:
                    self.update_merge_request(
                        attribute_dict={"add_labels": self.can_be_merged_label}
                    )
            else:
                if self.can_be_merged_label in self.merge_request.labels:
                    self.update_merge_request(
                        attribute_dict={"remove_labels": self.can_be_merged_label}
                    )
            return

        self.reset_verify_label()
        self.reset_reviewed_by_label()

    def process_approved_merge_request_webhook_data(self):
        if [
            self.username not in label for label in self.merge_request.labels
        ] or not self.merge_request.labels:
            self.add_remove_user_approve_label(action=ADD_STR)

    def process_unapproved_merge_request_webhook_data(self):
        if [self.username in label for label in self.merge_request.labels]:
            self.add_remove_user_approve_label(action=DELETE_STR)

    @property
    def approved_by_label(self):
        return f"{'Approved' if self.username in self.approvers else 'Reviewed'}{self.label_by_str}{self.username}"

    def add_remove_user_approve_label(self, action):
        self.app.logger.info(
            f"{self.repo_mr_log_message} {'Add' if action == ADD_STR else DELETE_STR} "
            f"approved label for {self.user['username']}"
        )

        if action == ADD_STR:
            self.add_update_label(
                project=self.repository,
                label_color=f"#{DYNAMIC_LABELS_DICT['approved-by-']}",
                label_name=self.approved_by_label,
            )
            self.update_merge_request(
                attribute_dict={"add_labels": [self.approved_by_label]}
            )
        if action == DELETE_STR:
            self.update_merge_request(
                attribute_dict={"remove_labels": [self.approved_by_label]}
            )

    def reset_reviewed_by_label(self):
        reviewed_by_labels = [
            label
            for label in self.merge_request.labels
            if self.label_by_str.lower() in label.lower()
        ]
        if reviewed_by_labels:
            self.update_merge_request(
                attribute_dict={"remove_labels": reviewed_by_labels}
            )

    def add_assignee(self):
        self.app.logger.info(f"{self.repo_mr_log_message} Adding PR owner as assignee")
        self.update_merge_request(
            attribute_dict={"assignee_id": self.merge_request.author["id"]}
        )

    def add_reviewers(self):
        # On GitLab's free tier, it is not possible to add more than one reviewer
        reviewers_list = [
            reviewer
            for reviewer in self.reviewers
            if reviewer != self.merge_request.author["username"]
        ]
        self.app.logger.info(
            f"{self.repo_mr_log_message} Adding reviewers {reviewers_list}"
        )
        reviewers_mention = " @".join(["", *reviewers_list])
        self.merge_request.notes.create({"body": reviewers_mention})

    def update_merge_request(self, attribute_dict):
        """
        attribute_dict: dict with merge request attribute to update
        https://docs.gitlab.com/ee/api/merge_requests.html#update-mr
        """
        self.app.logger.info(
            f"{self.repo_mr_log_message} Updating merge request: {attribute_dict}"
        )
        self.merge_request.manager.update(self.merge_request.get_id(), attribute_dict)

    def can_be_merged(self):
        """Checks if an MR can be merged.

        Returns True if PR is marked as verified and is approved by at least one maintainer and one reviewer and all
        threads are resolved
        """
        labels_prefix = ["Approved", "Reviewed", "verified"]
        merge_labels_labels = self.merge_request.labels
        self.app.logger.info(
            f"PR {self.repo_mr_log_message} labels: {merge_labels_labels}"
        )
        mr_labels_prefixes = [label.split("-")[0] for label in merge_labels_labels]

        mr_notes = self.merge_request.notes.list(get_all=True)
        all_threads_resolved = [
            note.attributes["resolved"]
            for note in mr_notes
            if note.attributes["resolvable"]
        ]

        return set(labels_prefix).issubset(set(mr_labels_prefixes)) and all(
            all_threads_resolved
        )

    @staticmethod
    def add_update_label(project, label_color, label_name):
        try:
            project.labels.update(
                name=label_name,
                new_data={"color": label_color},
            )
        except GitlabUpdateError:
            project.labels.create({"name": label_name, "color": label_color})
