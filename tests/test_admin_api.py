"""Tests for app.api.admin: user management with last-admin protections."""

from __future__ import annotations

from sqlalchemy import select

from app.core.auth.service import hash_password
from app.core.database.identity import AuditLog, Organization, User

ADMIN_EMAIL = "admin@example.com"


def _org_id(db_session) -> int:
    return (
        db_session.execute(select(User).where(User.email == ADMIN_EMAIL))
        .scalars()
        .first()
        .organization_id
    )


def _seed_user(
    db_session,
    email: str,
    role: str = "viewer",
    org=None,
    password: str = "correct-horse-battery",
) -> User:
    """Create a user the way the code allows: a real row with a hashed password.

    The public register route only ever creates an org's first admin, so
    additional members are seeded directly and then signed in through
    /auth/login like any other account.
    """
    user = User(
        organization_id=org if org is not None else _org_id(db_session),
        email=email,
        password_hash=hash_password(password),
        role=role,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


def _login(client, email: str, password: str = "correct-horse-battery") -> dict:
    response = client.post("/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['token']}"}


def _other_org(db_session, slug: str) -> Organization:
    org = Organization(name="Other Org", slug=slug, language="en")
    db_session.add(org)
    db_session.commit()
    db_session.refresh(org)
    return org


class TestListUsers:
    def test_admin_sees_only_own_organization(self, client, db_session, auth_headers):
        _seed_user(db_session, "member-a@example.com", role="viewer")
        outsider = _seed_user(
            db_session,
            "outsider@example.com",
            role="admin",
            org=_other_org(db_session, "other-org-list").id,
        )

        response = client.get("/admin/users", headers=auth_headers)
        assert response.status_code == 200
        body = response.json()
        emails = {u["email"] for u in body["users"]}
        # The other organization's admin must not leak into this tenant's list.
        assert emails == {ADMIN_EMAIL, "member-a@example.com"}
        assert outsider.email not in emails
        assert body["count"] == 2

        row = next(u for u in body["users"] if u["email"] == ADMIN_EMAIL)
        assert set(row) == {"id", "email", "role", "is_active", "organization_id"}
        assert row["role"] == "admin"
        assert row["is_active"] is True

    def test_search_filters_by_email_substring(self, client, db_session, auth_headers):
        _seed_user(db_session, "alice@example.com")
        _seed_user(db_session, "bob@example.com")

        response = client.get(
            "/admin/users", params={"search": "ali"}, headers=auth_headers
        )
        assert response.status_code == 200
        body = response.json()
        assert [u["email"] for u in body["users"]] == ["alice@example.com"]
        assert body["count"] == 1

    def test_limit_parameter_is_respected(self, client, db_session, auth_headers):
        for i in range(3):
            _seed_user(db_session, f"member-{i}@example.com")

        response = client.get("/admin/users", params={"limit": 2}, headers=auth_headers)
        assert response.status_code == 200
        body = response.json()
        assert body["count"] == 2
        assert len(body["users"]) == 2

    def test_limit_is_capped_at_200(self, client, db_session, auth_headers):
        org_id = _org_id(db_session)
        # No password hashing here: these rows only need to exist for the cap.
        bulk = [
            User(
                organization_id=org_id,
                email=f"bulk-{i}@example.com",
                password_hash="scrypt$00$00",
                role="viewer",
            )
            for i in range(201)
        ]
        db_session.add_all(bulk)
        db_session.commit()

        response = client.get("/admin/users", params={"limit": 500}, headers=auth_headers)
        assert response.status_code == 200
        body = response.json()
        # 201 seeded + the org admin, capped at 200.
        assert body["count"] == 200

    def test_non_admin_member_gets_403(self, client, db_session, auth_headers):
        _seed_user(db_session, "member@example.com", role="viewer")
        headers = _login(client, "member@example.com")

        response = client.get("/admin/users", headers=headers)
        assert response.status_code == 403
        assert "admin" in response.json()["detail"].lower()

    def test_no_token_gets_401(self, client):
        assert client.get("/admin/users").status_code == 401


class TestPatchUser:
    def test_patch_role_updates_row_and_audits(self, client, db_session, auth_headers):
        member = _seed_user(db_session, "member@example.com", role="viewer")

        response = client.patch(
            f"/admin/users/{member.id}", json={"role": "manager"}, headers=auth_headers
        )
        assert response.status_code == 200
        body = response.json()
        assert body["id"] == member.id
        assert body["email"] == "member@example.com"
        assert body["role"] == "manager"
        assert body["is_active"] is True

        db_session.refresh(member)
        assert member.role == "manager"

        rows = (
            db_session.execute(
                select(AuditLog).where(AuditLog.action == "admin.user_patch")
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1
        assert rows[0].resource == f"user:{member.id}"

    def test_patch_is_active_deactivates_member(self, client, db_session, auth_headers):
        member = _seed_user(db_session, "member@example.com")

        response = client.patch(
            f"/admin/users/{member.id}", json={"is_active": False}, headers=auth_headers
        )
        assert response.status_code == 200
        assert response.json()["is_active"] is False

        db_session.refresh(member)
        assert member.is_active is False

    def test_unknown_role_is_400(self, client, db_session, auth_headers):
        member = _seed_user(db_session, "member@example.com")

        response = client.patch(
            f"/admin/users/{member.id}", json={"role": "overlord"}, headers=auth_headers
        )
        assert response.status_code == 400
        assert "Unknown role" in response.json()["detail"]

    def test_missing_user_is_404(self, client, auth_headers):
        response = client.patch(
            "/admin/users/999999", json={"role": "viewer"}, headers=auth_headers
        )
        assert response.status_code == 404

    def test_other_organization_target_is_404(self, client, db_session, auth_headers):
        outsider = _seed_user(
            db_session,
            "outsider@example.com",
            role="admin",
            org=_other_org(db_session, "other-org-patch").id,
        )

        response = client.patch(
            f"/admin/users/{outsider.id}", json={"role": "viewer"}, headers=auth_headers
        )
        assert response.status_code == 404

    def _user(self, db_session) -> User:
        return (
            db_session.execute(select(User).where(User.email == ADMIN_EMAIL))
            .scalars()
            .first()
        )

    def test_demoting_the_last_admin_is_409(self, client, db_session, auth_headers):
        admin = self._user(db_session)

        response = client.patch(
            f"/admin/users/{admin.id}", json={"role": "manager"}, headers=auth_headers
        )
        assert response.status_code == 409
        assert "last active admin" in response.json()["detail"]

        db_session.refresh(admin)
        assert admin.role == "admin"

    def test_deactivating_the_last_admin_is_409(self, client, db_session, auth_headers):
        admin = self._user(db_session)

        response = client.patch(
            f"/admin/users/{admin.id}", json={"is_active": False}, headers=auth_headers
        )
        assert response.status_code == 409
        assert "last active admin" in response.json()["detail"]

        db_session.refresh(admin)
        assert admin.is_active is True

    def test_demoting_an_admin_is_allowed_with_a_second_admin(
        self, client, db_session, auth_headers
    ):
        _seed_user(db_session, "second-admin@example.com", role="admin")
        admin = self._user(db_session)

        response = client.patch(
            f"/admin/users/{admin.id}", json={"role": "viewer"}, headers=auth_headers
        )
        assert response.status_code == 200
        assert response.json()["role"] == "viewer"

        db_session.refresh(admin)
        assert admin.role == "viewer"

    def test_deactivating_an_admin_is_allowed_with_a_second_active_admin(
        self, client, db_session, auth_headers
    ):
        _seed_user(db_session, "second-admin@example.com", role="admin")
        admin = self._user(db_session)

        response = client.patch(
            f"/admin/users/{admin.id}", json={"is_active": False}, headers=auth_headers
        )
        assert response.status_code == 200
        assert response.json()["is_active"] is False

        db_session.refresh(admin)
        assert admin.is_active is False

    def test_non_admin_member_cannot_patch(self, client, db_session, auth_headers):
        member = _seed_user(db_session, "member@example.com", role="viewer")
        headers = _login(client, "member@example.com")

        response = client.patch(
            f"/admin/users/{member.id}", json={"role": "admin"}, headers=headers
        )
        assert response.status_code == 403

    def test_no_token_gets_401(self, client):
        response = client.patch("/admin/users/1", json={"role": "viewer"})
        assert response.status_code == 401

