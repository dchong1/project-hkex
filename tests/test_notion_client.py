"""Unit tests for Notion category tags and option-name sanitization (no network)."""

from __future__ import annotations

import json
import unittest
from unittest.mock import MagicMock, patch

from notion_client import (
    PROP_CATEGORY,
    parse_category_tags,
    sanitize_notion_option_name,
    create_announcement_page,
)


class SanitizeNotionOptionNameTests(unittest.TestCase):
    def test_replaces_commas_like_failed_hkex_esg_category(self) -> None:
        raw = (
            "Annual Report / Environmental, Social and Governance "
            "Information/Report] 2026 ANNUAL REPORT ( 23MB )"
        )
        out = sanitize_notion_option_name(raw)
        self.assertNotIn(",", out)
        self.assertIn("Environmental / Social and Governance", out)

    def test_collapses_whitespace_and_newlines(self) -> None:
        out = sanitize_notion_option_name("Line one,\nLine two")
        self.assertEqual(out, "Line one / Line two")

    def test_truncates_to_max_len(self) -> None:
        long_name = "A" * 150
        self.assertEqual(len(sanitize_notion_option_name(long_name)), 100)

    def test_empty_and_blank(self) -> None:
        self.assertEqual(sanitize_notion_option_name(""), "")
        self.assertEqual(sanitize_notion_option_name("   "), "")


class ParseCategoryTagsTests(unittest.TestCase):
    def test_splits_on_first_dash_only(self) -> None:
        tags = parse_category_tags("Announcements and Notices - [Inside Information]")
        self.assertEqual(tags, ["Announcements and Notices", "Inside Information"])

    def test_esg_headline_without_dash_is_single_tag(self) -> None:
        raw = (
            "Annual Report / Environmental, Social and Governance "
            "Information/Report] 2026 ANNUAL REPORT ( 23MB )"
        )
        tags = parse_category_tags(raw)
        self.assertEqual(len(tags), 1)
        self.assertIn(",", tags[0])


class CreateAnnouncementPageSanitizationTests(unittest.TestCase):
    @patch("notion_client.requests.post")
    def test_create_sends_comma_free_multi_select(self, mock_post: MagicMock) -> None:
        mock_post.return_value = MagicMock(status_code=200, json=lambda: {"id": "page-id"})

        category = (
            "Annual Report / Environmental, Social and Governance "
            "Information/Report] 2026 ANNUAL REPORT ( 23MB )"
        )
        create_announcement_page(
            "db-id",
            "token",
            {
                "document_title": "2026 ANNUAL REPORT",
                "category_text": category,
                "unique_id": "2026100601549",
                "stock_code": "00001",
                "company_short_name": "CKH",
                "pdf_url": "https://example.com/a.pdf",
            },
            summary="Test summary",
        )

        body = mock_post.call_args.kwargs["json"]
        multi = body["properties"][PROP_CATEGORY]["multi_select"]
        for opt in multi:
            self.assertNotIn(",", opt["name"], msg=json.dumps(multi))


if __name__ == "__main__":
    unittest.main()
