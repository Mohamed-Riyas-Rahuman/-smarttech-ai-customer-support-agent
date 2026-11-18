from unittest.mock import MagicMock

from app.services.sync_worker import _list_new_message_metas


def test_history_poll_returns_only_inbox_additions_and_deduplicates_ids():
    service = MagicMock()
    service.users.return_value.history.return_value.list.return_value.execute.side_effect = [
        {
            "historyId": "200",
            "history": [{"messagesAdded": [
                {"message": {"id": "new-inbox", "labelIds": ["INBOX", "UNREAD"]}},
                {"message": {"id": "old-read", "labelIds": ["INBOX"]}},
                {"message": {"id": "spam", "labelIds": ["INBOX", "SPAM"]}},
                {"message": {"id": "sent", "labelIds": ["SENT"]}},
            ]}],
            "nextPageToken": "page-2",
        },
        {
            "historyId": "201",
            "history": [{"messagesAdded": [
                {"message": {"id": "new-inbox", "labelIds": ["INBOX"]}},
                {"message": {"id": "later-inbox", "labelIds": ["INBOX"]}},
            ]}],
        },
    ]

    messages, cursor = _list_new_message_metas(service, "100")

    assert messages == [{"id": "new-inbox"}, {"id": "old-read"}, {"id": "later-inbox"}]
    assert cursor == "201"
    calls = service.users.return_value.history.return_value.list.call_args_list
    assert calls[0].kwargs["startHistoryId"] == "100"
    assert calls[1].kwargs["pageToken"] == "page-2"
