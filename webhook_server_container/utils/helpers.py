from __future__ import annotations
import datetime
from logging import Logger
import shlex
import subprocess
from concurrent.futures import Future, as_completed
from typing import Any, Dict, List, Optional, Tuple
from colorama import Fore
from github import Github
from github.RateLimit import RateLimit
from github.Repository import Repository
from simple_logger.logger import get_logger

from webhook_server_container.libs.config import Config


CONFIG = Config()


def get_value_from_dicts(
    primary_dict: Dict[Any, Any],
    secondary_dict: Dict[Any, Any],
    key: str,
    return_on_none: Optional[Any] = None,
) -> Any:
    """
    Get value from two dictionaries.

    If value is not found in primary_dict, try to get it from secondary_dict, otherwise return return_on_none.
    """
    return primary_dict.get(key, secondary_dict.get(key, return_on_none))


def get_logger_with_params(name: str, repository_name: Optional[str] = "") -> Logger:
    config_data = CONFIG.data  # Global repositories configuration
    repo_data: Dict[str, Any] = {}

    if repository_name:
        repo_data = CONFIG.repository_data(repository_name=repository_name)  # Specific repository configuration

    log_level: str = get_value_from_dicts(
        primary_dict=repo_data, secondary_dict=config_data, key="log-level", return_on_none="INFO"
    )
    log_file: str = get_value_from_dicts(primary_dict=repo_data, secondary_dict=config_data, key="log-file")
    return get_logger(name=name, filename=log_file, level=log_level)


LOGGER = get_logger_with_params(name="helpers")


def extract_key_from_dict(key: Any, _dict: Dict[Any, Any]) -> Any:
    if isinstance(_dict, dict):
        for _key, _val in _dict.items():
            if _key == key:
                yield _val
            if isinstance(_val, dict):
                for result in extract_key_from_dict(key, _val):
                    yield result
            elif isinstance(_val, list):
                for _item in _val:
                    for result in extract_key_from_dict(key, _item):
                        yield result


def get_github_repo_api(github_api: Github, repository: int | str) -> Repository:
    return github_api.get_repo(repository)


def run_command(
    command: str,
    log_prefix: str,
    verify_stderr: bool = False,
    shell: bool = False,
    timeout: Optional[int] = None,
    capture_output: bool = True,
    check: bool = False,
    **kwargs: Any,
) -> Tuple[bool, str, str]:
    """
    Run command locally.

    Args:
        command (str): Command to run
        log_prefix (str): Prefix for log messages
        verify_stderr (bool, default False): Check command stderr
        shell (bool, default False): run subprocess with shell toggle
        timeout (int, optional): Command wait timeout
        capture_output (bool, default True): Capture command output
        check (boot, default False):  If check is True and the exit code was non-zero, it raises a
            CalledProcessError

    Returns:
        tuple: True, out if command succeeded, False, err otherwise.
    """
    out_decoded: str = ""
    err_decoded: str = ""
    try:
        LOGGER.debug(f"{log_prefix} Running '{command}' command")
        sub_process = subprocess.run(
            shlex.split(command),
            capture_output=capture_output,
            check=check,
            shell=shell,
            text=True,
            timeout=timeout,
            **kwargs,
        )
        out_decoded = sub_process.stdout
        err_decoded = sub_process.stderr

        error_msg = (
            f"{log_prefix} Failed to run '{command}'. "
            f"rc: {sub_process.returncode}, out: {out_decoded}, error: {err_decoded}"
        )

        if sub_process.returncode != 0:
            LOGGER.error(error_msg)
            return False, out_decoded, err_decoded

        # From this point and onwards we are guaranteed that sub_process.returncode == 0
        if err_decoded and verify_stderr:
            LOGGER.error(error_msg)
            return False, out_decoded, err_decoded

        return True, out_decoded, err_decoded
    except Exception as ex:
        LOGGER.error(f"{log_prefix} Failed to run '{command}' command: {ex}")
        return False, out_decoded, err_decoded


def get_apis_and_tokes_from_config(config: Config, repository_name: str = "") -> List[Tuple[Github, str]]:
    apis_and_tokens: List[Tuple[Github, str]] = []

    tokens = get_value_from_dicts(
        primary_dict=config.repository_data(repository_name=repository_name),
        secondary_dict=config.data,
        key="github-tokens",
        return_on_none=[],
    )

    for _token in tokens:
        apis_and_tokens.append((Github(login_or_token=_token), _token))

    return apis_and_tokens


def get_api_with_highest_rate_limit(config: Config, repository_name: str = "") -> Tuple[Github | None, str | None]:
    """
    Get API with the highest rate limit

    Args:
        config (Config): Config object
        repository_name (str, optional): Repository name, if provided try to get token set in config repository section.

    Returns:
        tuple: API, token
    """
    api: Optional[Github] = None
    token: Optional[str] = None
    _api_user: str = ""
    rate_limit: Optional[RateLimit] = None

    remaining = 0

    apis_and_tokens = get_apis_and_tokes_from_config(config=config, repository_name=repository_name)
    for _api, _token in apis_and_tokens:
        _api_user = _api.get_user().login
        rate_limit = _api.get_rate_limit()
        if rate_limit.core.remaining > remaining:
            remaining = rate_limit.core.remaining
            LOGGER.debug(f"API user {_api_user} remaining rate limit: {remaining}")
            api, token = _api, _token

    if rate_limit:
        log_rate_limit(rate_limit=rate_limit, api_user=_api_user)

    LOGGER.info(f"API user {_api_user} selected with highest rate limit: {remaining}")
    return api, token


def log_rate_limit(rate_limit: RateLimit, api_user: str) -> None:
    rate_limit_str: str
    time_for_limit_reset: int = (rate_limit.core.reset - datetime.datetime.now(tz=datetime.timezone.utc)).seconds
    below_minimum: bool = rate_limit.core.remaining < 700

    if below_minimum:
        rate_limit_str = f"{Fore.RED}{rate_limit.core.remaining}{Fore.RESET}"

    elif rate_limit.core.remaining < 2000:
        rate_limit_str = f"{Fore.YELLOW}{rate_limit.core.remaining}{Fore.RESET}"

    else:
        rate_limit_str = f"{Fore.GREEN}{rate_limit.core.remaining}{Fore.RESET}"

    msg = (
        f"{Fore.CYAN}[{api_user}] API rate limit:{Fore.RESET} Current {rate_limit_str} of {rate_limit.core.limit}. "
        f"Reset in {rate_limit.core.reset} [{datetime.timedelta(seconds=time_for_limit_reset)}] "
        f"(UTC time is {datetime.datetime.now(tz=datetime.timezone.utc)})"
    )
    LOGGER.debug(msg)
    if below_minimum:
        LOGGER.warning(msg)


def get_future_results(futures: List["Future"]) -> None:
    """
    result must return Tuple[bool, str, Callable] when the Callable is Logger function (LOGGER.info, LOGGER.error, etc)
    """
    for result in as_completed(futures):
        _res = result.result()
        _log = _res[2]
        if result.exception():
            _log(result.exception())

        if _res[0]:
            _log(_res[1])

        else:
            _log(_res[1])
