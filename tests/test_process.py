import pytest
from unittest.mock import MagicMock, patch

from process import fetch_enriched, mark_processed, sync_quick_categories


class TestFetchEnriched:
    def test_returns_json_on_success(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = [{"id": "tx_001"}, {"id": "tx_002"}]
        with patch("process.requests.get", return_value=mock_resp) as mock_get:
            result = fetch_enriched()
        assert result == [{"id": "tx_001"}, {"id": "tx_002"}]
        mock_get.assert_called_once()
        mock_resp.raise_for_status.assert_called_once()

    def test_includes_api_key_header(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = []
        with patch("process.requests.get", return_value=mock_resp) as mock_get:
            fetch_enriched()
        _, kwargs = mock_get.call_args
        assert "X-API-Key" in kwargs.get("headers", {})

    def test_hits_export_endpoint(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = []
        with patch("process.requests.get", return_value=mock_resp) as mock_get:
            fetch_enriched()
        url = mock_get.call_args[0][0]
        assert url.endswith("/export")

    def test_raises_on_http_error(self):
        mock_resp = MagicMock()
        mock_resp.raise_for_status.side_effect = Exception("HTTP 401 Unauthorized")
        with patch("process.requests.get", return_value=mock_resp):
            with pytest.raises(Exception, match="401"):
                fetch_enriched()

    def test_returns_empty_list(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = []
        with patch("process.requests.get", return_value=mock_resp):
            assert fetch_enriched() == []


class TestMarkProcessed:
    def test_posts_ids_in_body(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True}
        with patch("process.requests.post", return_value=mock_resp) as mock_post:
            result = mark_processed(["tx_001", "tx_002"])
        assert result == {"ok": True}
        _, kwargs = mock_post.call_args
        assert kwargs["json"] == {"ids": ["tx_001", "tx_002"]}

    def test_includes_api_key_header(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {}
        with patch("process.requests.post", return_value=mock_resp) as mock_post:
            mark_processed(["tx_001"])
        _, kwargs = mock_post.call_args
        assert "X-API-Key" in kwargs.get("headers", {})

    def test_hits_mark_processed_endpoint(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {}
        with patch("process.requests.post", return_value=mock_resp) as mock_post:
            mark_processed(["tx_001"])
        url = mock_post.call_args[0][0]
        assert "mark-processed" in url

    def test_raises_on_http_error(self):
        mock_resp = MagicMock()
        mock_resp.raise_for_status.side_effect = Exception("HTTP 500")
        with patch("process.requests.post", return_value=mock_resp):
            with pytest.raises(Exception, match="500"):
                mark_processed(["tx_001"])

    def test_empty_ids_list(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True}
        with patch("process.requests.post", return_value=mock_resp) as mock_post:
            mark_processed([])
        _, kwargs = mock_post.call_args
        assert kwargs["json"] == {"ids": []}


class TestSyncQuickCategories:
    _global_rows = [{"category": "Food & Drink", "subcategory": "Groceries", "transaction_count": 10}]
    _merchant_rows = [
        {"merchant_name": "Tesco", "category": "Food & Drink", "subcategory": "Groceries",
         "transaction_count": 5, "rank": 1},
        {"merchant_name": "Tesco", "category": "Food & Drink", "subcategory": "Snacks",
         "transaction_count": 2, "rank": 2},
    ]

    def test_combines_global_and_merchant_entries(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"synced": 3}
        with patch("process.get_top_subcategories", return_value=self._global_rows), \
             patch("process.get_top_merchant_subcategories", return_value=self._merchant_rows), \
             patch("process.requests.post", return_value=mock_resp) as mock_post:
            result = sync_quick_categories()

        assert result == {"synced": 3}
        _, kwargs = mock_post.call_args
        entries = kwargs["json"]["entries"]
        assert entries == [
            {"category": "Food & Drink", "subcategory": "Groceries", "merchant_name": None, "rank": 0},
            {"category": "Food & Drink", "subcategory": "Groceries", "merchant_name": "Tesco", "rank": 0},
            {"category": "Food & Drink", "subcategory": "Snacks", "merchant_name": "Tesco", "rank": 1},
        ]

    def test_hits_sync_endpoint(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {}
        with patch("process.get_top_subcategories", return_value=[]), \
             patch("process.get_top_merchant_subcategories", return_value=[]), \
             patch("process.requests.post", return_value=mock_resp) as mock_post:
            sync_quick_categories()
        url = mock_post.call_args[0][0]
        assert url.endswith("/sync-quick-categories")

    def test_includes_api_key_header(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {}
        with patch("process.get_top_subcategories", return_value=[]), \
             patch("process.get_top_merchant_subcategories", return_value=[]), \
             patch("process.requests.post", return_value=mock_resp) as mock_post:
            sync_quick_categories()
        _, kwargs = mock_post.call_args
        assert "X-API-Key" in kwargs.get("headers", {})

    def test_raises_on_http_error(self):
        mock_resp = MagicMock()
        mock_resp.raise_for_status.side_effect = Exception("HTTP 500")
        with patch("process.get_top_subcategories", return_value=[]), \
             patch("process.get_top_merchant_subcategories", return_value=[]), \
             patch("process.requests.post", return_value=mock_resp):
            with pytest.raises(Exception, match="500"):
                sync_quick_categories()

    def test_empty_when_no_data(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"synced": 0}
        with patch("process.get_top_subcategories", return_value=[]), \
             patch("process.get_top_merchant_subcategories", return_value=[]), \
             patch("process.requests.post", return_value=mock_resp) as mock_post:
            sync_quick_categories()
        _, kwargs = mock_post.call_args
        assert kwargs["json"]["entries"] == []
