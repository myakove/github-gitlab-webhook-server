import os
import pytest
from webhook_server_container.libs.config import Config
from webhook_server_container.utils.github_repository_settings import (
    get_repo_branch_protection_rules,
    DEFAULT_BRANCH_PROTECTION,
)


@pytest.fixture()
def branch_protection_rules(request, mocker):
    os.environ["WEBHOOK_SERVER_DATA_DIR"] = "webhook_server_container/tests/manifests"
    config = Config()
    repo_name = "test-repo"
    data = config.data
    for key in request.param.get("global", {}):
        data[key] = request.param["global"][key]
    for key in request.param.get("repo", {}):
        data["repositories"][repo_name][key] = request.param["repo"][key]
    mocker.patch(
        "webhook_server_container.libs.config.Config.data", new_callable=mocker.PropertyMock, return_value=data
    )
    return get_repo_branch_protection_rules(config_data=config.data, repo_data=config.data["repositories"][repo_name])


@pytest.mark.parametrize(
    "branch_protection_rules, expected",
    [
        pytest.param(
            {
                "global": {
                    "strict": True,
                },
                "repo": {
                    "strict": False,
                },
            },
            {
                "strict": False,
            },
            id="test_repo_branch_protection_rule",
        ),
        pytest.param(
            {
                "global": {
                    "strict": False,
                },
            },
            {
                "strict": False,
            },
            id="test_global_branch_protection_rule",
        ),
        pytest.param(
            {
                "global": {
                    "strict": False,
                    "require_code_owner_reviews": True,
                    "dismiss_stale_reviews": False,
                    "required_approving_review_count": 2,
                    "required_linear_history": False,
                },
                "repo": {
                    "strict": True,
                    "require_code_owner_reviews": True,
                    "dismiss_stale_reviews": False,
                    "required_approving_review_count": 1,
                    "required_linear_history": True,
                },
            },
            {
                "strict": True,
                "require_code_owner_reviews": True,
                "dismiss_stale_reviews": False,
                "required_approving_review_count": 1,
                "required_linear_history": True,
            },
            id="test_repo_multiple_branch_protection_rule",
        ),
        pytest.param(
            {},
            {
                **DEFAULT_BRANCH_PROTECTION,
            },
            id="test_default_branch_protection_rule",
        ),
    ],
    indirect=["branch_protection_rules"],
)
def test_branch_protection_setup(branch_protection_rules, expected):
    mismatch = {}
    for key in expected:
        if branch_protection_rules[key] != expected[key]:
            mismatch[key] = f"Expected value for {key}: {expected[key]}, actual: {branch_protection_rules[key]}"
    assert not mismatch, f"Following mismatches are found: {mismatch}"
