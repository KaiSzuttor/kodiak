import textwrap
from dataclasses import dataclass
from enum import Enum, auto
from html.parser import HTMLParser
from typing import List, Optional, Tuple, cast

import structlog
from markdown_html_finder import find_html_positions

import kodiak.app_config as conf
from kodiak import messages, queries
from kodiak.config import V1, BodyText, MergeBodyStyle, MergeTitleStyle
from kodiak.config_utils import get_markdown_for_config
from kodiak.errors import (
    BranchMerged,
    MergeBlocked,
    MergeConflict,
    MissingAppID,
    MissingGithubMergeabilityState,
    MissingSkippableChecks,
    NeedsBranchUpdate,
    NotQueueable,
    WaitingForChecks,
)
from kodiak.evaluation import mergeable
from kodiak.queries import EventInfoResponse, PullRequest, get_headers

logger = structlog.get_logger()

CONFIG_FILE_PATH = ".kodiak.toml"


class MergeabilityResponse(Enum):
    OK = auto()
    NEEDS_UPDATE = auto()
    NEED_REFRESH = auto()
    NOT_MERGEABLE = auto()
    SKIPPABLE_CHECKS = auto()
    WAIT = auto()


class CommentHTMLParser(HTMLParser):
    # define this attribute to make mypy accept `self.offset`
    offset: int

    def __init__(self) -> None:
        self.comments: List[Tuple[int, int]] = []
        super().__init__()

    def handle_comment(self, tag: str) -> None:
        start_token_len = len("<!--")
        end_token_len = len("-->")
        tag_len = len(tag)
        end = start_token_len + tag_len + end_token_len
        self.comments.append((self.offset, end + self.offset))

    def reset(self) -> None:
        self.comments = []
        super().reset()


html_parser = CommentHTMLParser()


def strip_html_comments_from_markdown(raw_message: str) -> str:
    """
    1. parse string into a markdown AST
    2. find the HTML nodes
    3. parse HTML nodes into HTML
    4. find comments in HTML
    5. slice out comments from original message
    """
    # NOTE(chdsbd): Remove carriage returns so find_html_positions can process
    # html correctly. pulldown-cmark doesn't handle carriage returns well.
    # remark-parse also doesn't handle carriage returns:
    # https://github.com/remarkjs/remark/issues/195#issuecomment-230760892
    message = raw_message.replace("\r", "")
    html_node_positions = find_html_positions(message)
    comment_locations = []
    for html_start, html_end in html_node_positions:
        html_text = message[html_start:html_end]
        html_parser.feed(html_text)
        for comment_start, comment_end in html_parser.comments:
            comment_locations.append(
                (html_start + comment_start, html_start + comment_end)
            )
        html_parser.reset()

    new_message = message
    for comment_start, comment_end in reversed(comment_locations):
        new_message = new_message[:comment_start] + new_message[comment_end:]
    return new_message


def get_body_content(
    body_type: BodyText, strip_html_comments: bool, pull_request: PullRequest
) -> str:
    if body_type == BodyText.markdown:
        body = pull_request.body
        if strip_html_comments:
            return strip_html_comments_from_markdown(body)
        return body
    if body_type == BodyText.plain_text:
        return pull_request.bodyText
    if body_type == BodyText.html:
        return pull_request.bodyHTML
    raise Exception(f"Unknown body_type: {body_type}")


EMPTY_STRING = ""


def get_merge_body(config: V1, pull_request: PullRequest) -> dict:
    merge_body: dict = {"merge_method": config.merge.method.value}
    if config.merge.message.body == MergeBodyStyle.pull_request_body:
        body = get_body_content(
            config.merge.message.body_type,
            config.merge.message.strip_html_comments,
            pull_request,
        )
        merge_body.update(dict(commit_message=body))
    if config.merge.message.body == MergeBodyStyle.empty:
        merge_body.update(dict(commit_message=EMPTY_STRING))
    if config.merge.message.title == MergeTitleStyle.pull_request_title:
        merge_body.update(dict(commit_title=pull_request.title))
    if config.merge.message.include_pr_number and merge_body.get("commit_title"):
        merge_body["commit_title"] += f" (#{pull_request.number})"
    return merge_body


def create_git_revision_expression(branch: str, file_path: str) -> str:
    return f"{branch}:{file_path}"


@dataclass(init=False, repr=False, eq=False)
class PR:
    number: int
    owner: str
    repo: str
    installation_id: str
    log: structlog.BoundLogger
    event: Optional[EventInfoResponse]
    client: queries.Client

    def __eq__(self, b: object) -> bool:
        if not isinstance(b, PR):
            raise NotImplementedError
        return (
            self.number == b.number
            and self.owner == b.owner
            and self.repo == b.repo
            and self.installation_id == b.installation_id
        )

    def __init__(
        self,
        number: int,
        owner: str,
        repo: str,
        installation_id: str,
        client: queries.Client,
    ):
        self.number = number
        self.owner = owner
        self.repo = repo
        self.installation_id = installation_id
        self.client = client
        self.event = None
        self.log = logger.bind(repo=f"{owner}/{repo}#{number}")

    def __repr__(self) -> str:
        return f"<PR path='{self.owner}/{self.repo}#{self.number}'>"

    async def get_event(self) -> Optional[EventInfoResponse]:
        default_branch_name = await self.client.get_default_branch_name()
        if default_branch_name is None:
            return None
        return await self.client.get_event_info(
            config_file_expression=create_git_revision_expression(
                branch=default_branch_name, file_path=CONFIG_FILE_PATH
            ),
            pr_number=self.number,
        )

    async def set_status(
        self,
        summary: str,
        detail: Optional[str] = None,
        markdown_content: Optional[str] = None,
    ) -> None:
        """
        Display a message to a user through a github check

        `summary` and `detail` work to build the message displayed alongside
        other status checks on the PR. They format a message like: '<summary> (<detail>)'

        `markdown_content` is the message displayed on the detail view for a
        status check. This detail view is accessible via the "Details" link
        alongside the summary/detail content.
        """
        if detail is not None:
            message = f"{summary} ({detail})"
        else:
            message = summary
        if self.event is None:
            self.log.info("missing event. attempting to fetch it.")
            self.event = await self.get_event()
        if self.event is None:
            self.log.error("could not fetch event")
            return
        self.log.info("setting status %s", message)
        await self.client.create_notification(
            head_sha=self.event.pull_request.latest_sha,
            message=message,
            summary=markdown_content,
        )

    # TODO(chdsbd): Move set_status updates out of this method
    async def mergeability(
        self, merging: bool = False
    ) -> Tuple[MergeabilityResponse, Optional[EventInfoResponse]]:
        self.log.info("get_event")
        self.event = await self.get_event()
        if self.event is None:
            self.log.info("no event")
            return MergeabilityResponse.NOT_MERGEABLE, None
        # PRs from forks will always appear deleted because the v4 api doesn't
        # return head information for forks like the v3 api does.
        if not self.event.pull_request.isCrossRepository and not self.event.head_exists:
            self.log.info("branch deleted")
            return MergeabilityResponse.NOT_MERGEABLE, None
        if not isinstance(self.event.config, V1):

            await self.set_status(
                "🚨 Invalid configuration",
                detail='Click "Details" for more info.',
                markdown_content=get_markdown_for_config(
                    self.event.config,
                    self.event.config_str,
                    self.event.config_file_expression,
                ),
            )
            return MergeabilityResponse.NOT_MERGEABLE, None
        try:
            self.log.info("check mergeable")
            mergeable(
                config=self.event.config,
                app_id=conf.GITHUB_APP_ID,
                pull_request=self.event.pull_request,
                branch_protection=self.event.branch_protection,
                review_requests=self.event.review_requests,
                reviews=self.event.reviews,
                contexts=self.event.status_contexts,
                check_runs=self.event.check_runs,
                valid_signature=self.event.valid_signature,
                valid_merge_methods=self.event.valid_merge_methods,
            )
            self.log.info("okay")
            return MergeabilityResponse.OK, self.event
        except MissingSkippableChecks as e:
            self.log.info("skippable checks", checks=e.checks)
            await self.set_status(
                summary="🛑 not waiting for dont_wait_on_status_checks",
                detail=repr(e.checks),
            )
            return MergeabilityResponse.SKIPPABLE_CHECKS, self.event
        except (NotQueueable, MergeConflict, BranchMerged) as e:
            self.log.info("not queueable, mergeconflict, or branch merged")
            if (
                isinstance(e, MergeConflict)
                and self.event.config.merge.notify_on_conflict
            ):
                await self.notify_pr_creator()

            if (
                isinstance(e, BranchMerged)
                and self.event.config.merge.delete_branch_on_merge
            ):
                await self.client.delete_branch(
                    branch=self.event.pull_request.headRefName
                )

            await self.set_status(summary="🛑 cannot merge", detail=str(e))
            return MergeabilityResponse.NOT_MERGEABLE, self.event
        except MergeBlocked as e:
            await self.set_status(summary=f"🛑 {e}")
            return MergeabilityResponse.NOT_MERGEABLE, self.event
        except MissingAppID:
            return MergeabilityResponse.NOT_MERGEABLE, self.event
        except MissingGithubMergeabilityState:
            self.log.info("missing mergeability state, need refresh")
            return MergeabilityResponse.NEED_REFRESH, self.event
        except WaitingForChecks as e:
            if merging:
                await self.set_status(
                    summary="⛴ attempting to merge PR",
                    detail=f"waiting for checks: {e.checks!r}",
                )
            return MergeabilityResponse.WAIT, self.event
        except NeedsBranchUpdate:
            if self.event.pull_request.isCrossRepository:
                await self.set_status(
                    summary='🚨 forks cannot be updated via the github api. Click "Details" for more info',
                    markdown_content=messages.FORKS_CANNOT_BE_UPDATED,
                )
                return MergeabilityResponse.NOT_MERGEABLE, self.event
            if merging:
                await self.set_status(
                    summary="⛴ attempting to merge PR", detail="updating branch"
                )
            return MergeabilityResponse.NEEDS_UPDATE, self.event

    async def update(self) -> bool:
        self.log.info("update")
        event = await self.get_event()
        if event is None:
            self.log.warning("problem")
            return False
        res = await self.client.merge_branch(
            head=event.pull_request.baseRefName, base=event.pull_request.headRefName
        )
        if res.status_code > 300:
            self.log.error("could not update branch", res=res, res_json=res.json())
            return False
        return True

    async def trigger_mergeability_check(self) -> None:
        await self.client.get_pull_request(number=self.number)

    async def merge(self, event: EventInfoResponse) -> bool:
        if not isinstance(event.config, V1):
            self.log.error("we should never have a config error when we call merge")
            return False

        res = await self.client.merge_pull_request(
            number=self.number, body=get_merge_body(event.config, event.pull_request)
        )
        if res.status_code > 300:
            self.log.info("could not merge PR", res=res, res_json=res.json())
            return False
        return True

    async def delete_label(self, label: str) -> bool:
        """
        remove the PR label specified by `label_id` for a given `pr_number`
        """
        self.log.info("deleting label", label=label)
        headers = await get_headers(installation_id=self.installation_id)
        res = await self.client.session.delete(
            f"https://api.github.com/repos/{self.owner}/{self.repo}/issues/{self.number}/labels/{label}",
            headers=headers,
        )
        return cast(bool, res.status_code != 204)

    async def create_comment(self, body: str) -> bool:
        """
        create a comment on the speicifed `pr_number` with the given `body` as text.
        """
        self.log.info("creating comment", body=body)
        headers = await get_headers(installation_id=self.installation_id)
        res = await self.client.session.post(
            f"https://api.github.com/repos/{self.owner}/{self.repo}/issues/{self.number}/comments",
            json=dict(body=body),
            headers=headers,
        )
        return cast(bool, res.status_code != 200)

    async def notify_pr_creator(self) -> bool:
        """
        comment on PR with an `@$PR_CREATOR_NAME` and remove `automerge` label.

        Since we don't have atomicity we chose to remove the label first
        instead of creating the comment first as we would rather have no
        comment instead of multiple comments on each consecutive PR push.
        """

        event = self.event
        if not event:
            return False
        if not isinstance(event.config, V1):
            self.log.error("config attribute was not a config")
            return False

        if not event.config.merge.require_automerge_label:
            return False

        label = event.config.merge.automerge_label
        if not await self.delete_label(label=label):
            return False

        # TODO(sbdchd): add mentioning of PR author in comment.
        body = textwrap.dedent(
            f"""
        This PR currently has a merge conflict. Please resolve this and then re-add the `{label}` label.
        """
        )
        return await self.create_comment(body)
