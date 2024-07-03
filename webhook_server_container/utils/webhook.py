from concurrent.futures import ThreadPoolExecutor
import os
from typing import Any, Dict, List, Tuple

from github import Github, HookDescription
from simple_logger.logger import get_logger

from webhook_server_container.libs.config import Config
from webhook_server_container.utils.helpers import (
    get_api_with_highest_rate_limit,
    get_future_results,
    get_github_repo_api,
)
from pyhelper_utils.general import ignore_exceptions


LOGGER = get_logger(name="webhook", filename=os.environ.get("WEBHOOK_SERVER_LOG_FILE"))


@ignore_exceptions(logger=LOGGER)
def process_github_webhook(data: Dict[str, Any], github_api: Github, webhook_ip: str) -> Tuple[bool, str]:
    repository: str = data["name"]
    repo = get_github_repo_api(github_api=github_api, repository=repository)
    if not repo:
        return False, f"Could not find repository {repository}"

    config_: Dict[str, str] = {"url": f"{webhook_ip}/webhook_server", "content_type": "json"}
    events: List[str] = data.get("events", ["*"])

    try:
        hooks: List[HookDescription] = list(repo.get_hooks())
    except Exception as ex:
        return False, f"Could not list webhook for {repository}, check token permissions: {ex}"

    for _hook in hooks:
        if webhook_ip in _hook.config["url"]:
            return True, f"{repository}: Hook already exists - {_hook.config['url']}"

    LOGGER.info(f"Creating webhook: {config_['url']} for {repository} with events: {events}")
    repo.create_hook(name="web", config=config_, events=events, active=True)
    return True, f"{repository}: Create webhook is done"


def create_webhook(config_: Config, github_api: Github) -> None:
    LOGGER.info("Preparing webhook configuration")
    webhook_ip = config_.data["webhook_ip"]

    futures = []
    with ThreadPoolExecutor() as executor:
        for repo, data in config_.data["repositories"].items():
            futures.append(
                executor.submit(
                    process_github_webhook,
                    **{"data": data, "github_api": github_api, "webhook_ip": webhook_ip},
                )
            )

    get_future_results(futures=futures)


if __name__ == "__main__":
    config = Config()
    api, _ = get_api_with_highest_rate_limit(config=config)
    create_webhook(config_=config, github_api=api)
