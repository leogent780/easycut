from shorts_factory.pipeline import quota


def test_remaining_quota_starts_full(db_session):
    assert quota.remaining_quota(db_session, "proj1") == quota.DAILY_QUOTA_BUDGET


def test_spend_reduces_remaining(db_session):
    quota.spend(db_session, "proj1", 1600)
    db_session.commit()
    assert quota.remaining_quota(db_session, "proj1") == quota.DAILY_QUOTA_BUDGET - 1600


def test_has_budget_for_upload(db_session):
    assert quota.has_budget_for_upload(db_session, "proj1") is True
    quota.spend(db_session, "proj1", quota.DAILY_QUOTA_BUDGET - 1000)
    db_session.commit()
    assert quota.has_budget_for_upload(db_session, "proj1") is False  # only 1000 left, need 1600


def test_convenience_spend_wrappers_use_correct_costs(db_session):
    quota.spend_search_list(db_session, "proj1")  # 100
    quota.spend_videos_list(db_session, "proj1")  # 1
    quota.spend_videos_insert(db_session, "proj1")  # 1600
    db_session.commit()
    used = quota.DAILY_QUOTA_BUDGET - quota.remaining_quota(db_session, "proj1")
    assert used == 100 + 1 + 1600


def test_quota_isolated_per_project(db_session):
    quota.spend(db_session, "proj1", 5000)
    db_session.commit()
    assert quota.remaining_quota(db_session, "proj1") == quota.DAILY_QUOTA_BUDGET - 5000
    assert quota.remaining_quota(db_session, "proj2") == quota.DAILY_QUOTA_BUDGET
