import asyncio

from utils import get_github_repo_api, get_repository_from_config


async def set_branch_protection(app, branch, repository, required_status_checks):
    app.logger.info(f"Set repository {repository} branch {branch} settings")
    try:
        branch.edit_protection(
            strict=True,
            contexts=required_status_checks,
            require_code_owner_reviews=True,
            required_approving_review_count=1,
            dismiss_stale_reviews=True,
        )
    except Exception:
        return


async def process_github_webhook(app, data):
    protected_branches = data.get("protected-branches", [])
    if not protected_branches:
        return

    repository = data["name"]
    token = data["token"]
    repo = get_github_repo_api(app=app, token=token, repository=repository)
    if not repo or repo.private:
        return

    default_status_checks = [
        "pre-commit.ci - pr",
        "WIP",
        "dpulls",
        "SonarCloud Code Analysis",
        "Inclusive Language",
    ]

    tasks = []
    for branch_name, status_checks in protected_branches.items():
        branch = repo.get_branch(branch=branch_name)
        required_status_checks = []
        if data.get("verified_job"):
            required_status_checks.append("Verified")

        if data.get("tox"):
            required_status_checks.append("tox")

        if status_checks:
            required_status_checks.extend(status_checks)
        else:
            required_status_checks.extend(default_status_checks)

        tasks.append(
            asyncio.create_task(
                set_branch_protection(
                    app=app,
                    branch=branch,
                    repository=repository,
                    required_status_checks=required_status_checks,
                )
            )
        )
    for coro in asyncio.as_completed(tasks):
        await coro


async def set_repository_settings(app):
    app.logger.info("Set repository settings")
    repos = get_repository_from_config()

    tasks = []
    for repo, data in repos["repositories"].items():
        tasks.append(
            asyncio.create_task(
                process_github_webhook(
                    app=app,
                    data=data,
                )
            )
        )

    for coro in asyncio.as_completed(tasks):
        await coro
