import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex, CreateTable

from main import app
from qijia_video import auth, run_service
from qijia_video.db_models import VideoResource, VideoRun, WorkbenchUser
from qijia_video.settings import settings


class StandaloneAuthenticationTests(unittest.TestCase):
    def setUp(self):
        self.patches = [
            patch.object(settings, "ADMIN_USERNAME", "admin"),
            patch.object(settings, "ADMIN_PASSWORD", "correct-horse-battery"),
            patch.object(settings, "SESSION_SECRET", "s" * 48),
            patch.object(settings, "AUTH_COOKIE_SECURE", True),
        ]
        for item in self.patches:
            item.start()
        self.client = TestClient(app, base_url="https://testserver")

    def tearDown(self):
        self.client.close()
        for item in reversed(self.patches):
            item.stop()

    def test_health_is_public_but_paid_api_requires_login(self):
        self.assertEqual(self.client.get("/health").status_code, 200)
        response = self.client.get(
            "/api/qijia-video/capabilities", follow_redirects=False
        )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["message"], "请先登录")

    def test_login_sets_secure_cookie_and_opens_workbench(self):
        response = self.client.post(
            "/login",
            data={
                "username": "admin",
                "password": "correct-horse-battery",
                "next": "/qijia-video",
            },
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/qijia-video")
        cookie = response.headers["set-cookie"]
        self.assertIn("HttpOnly", cookie)
        self.assertIn("Secure", cookie)
        self.assertIn("SameSite=lax", cookie)
        workbench = self.client.get("/qijia-video")
        self.assertEqual(workbench.status_code, 200)
        self.assertIn(
            'data-admin-only>账号管理</a>',
            workbench.text,
        )
        self.assertEqual(workbench.headers["cache-control"], "no-store")
        self.assertEqual(
            self.client.get("/qijia-video/accounts").status_code,
            200,
        )
        costs = self.client.get("/qijia-video/costs")
        self.assertEqual(costs.status_code, 200)
        self.assertIn("内容生产成本分析", costs.text)
        self.assertEqual(costs.headers["cache-control"], "no-store")

    def test_wrong_password_fails_without_a_session(self):
        response = self.client.post(
            "/login",
            data={"username": "admin", "password": "wrong", "next": "/"},
        )
        self.assertEqual(response.status_code, 401)
        self.assertIn("账号或密码不正确", response.text)
        self.assertNotIn(auth.AUTH_COOKIE, self.client.cookies)

    def test_external_next_url_is_rejected(self):
        for target in (
            "https://attacker.example",
            "//attacker.example",
            "/\\attacker.example",
        ):
            with self.subTest(target=target):
                response = self.client.post(
                    "/login",
                    data={
                        "username": "admin",
                        "password": "correct-horse-battery",
                        "next": target,
                    },
                    follow_redirects=False,
                )
                self.assertEqual(
                    response.headers["location"], "/qijia-video"
                )

    def test_cost_page_is_an_allowed_post_login_destination(self):
        response = self.client.post(
            "/login",
            data={
                "username": "admin",
                "password": "correct-horse-battery",
                "next": "/qijia-video/costs?days=90",
            },
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)
        self.assertEqual(
            response.headers["location"], "/qijia-video/costs?days=90"
        )


class StandaloneRunServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_local_run_lifecycle_keeps_the_public_contract(self):
        with patch.object(run_service, "async_session", None):
            tokens = run_service.set_task_context({
                "id": 1,
                "username": "admin",
                "role": "admin",
            })
            try:
                name = "standalone-test-run"
                task_id, reused = await run_service.create_or_get_running_task_async(
                    name,
                    job_kind="qijia_video.run",
                    job_payload={"action": "generate_script", "job_id": "job-1"},
                    recoverable=True,
                )
                same_id, same_reused = (
                    await run_service.create_or_get_running_task_async(
                        name,
                        job_kind="qijia_video.run",
                        recoverable=True,
                    )
                )
                self.assertFalse(reused)
                self.assertTrue(same_reused)
                self.assertEqual(task_id, same_id)
                run_service.update_progress(task_id, {
                    "message": "正在生成脚本…",
                    "stage": "script_generation",
                    "percent": 14,
                })
                run_service.complete_task(task_id, {"job_id": "job-1"})
                task = await run_service.get_task_async(task_id)
            finally:
                run_service.reset_task_context(tokens)

        self.assertEqual(task["status"], "done")
        self.assertEqual(task["progress_meta"]["percent"], 14)
        public = run_service.public_task(task)
        self.assertNotIn("job_payload", public)
        self.assertNotIn("owner_user_id", public)

    def test_database_schema_has_resource_run_and_user_tables(self):
        self.assertEqual(VideoResource.__tablename__, "video_resources")
        self.assertEqual(VideoRun.__tablename__, "video_runs")
        self.assertEqual(WorkbenchUser.__tablename__, "qijia_users")
        self.assertIn("ux_video_runs_active_name_owner", {
            index.name for index in VideoRun.__table__.indexes
        })
        engine = create_engine("sqlite://")
        try:
            VideoResource.metadata.create_all(engine)
        finally:
            engine.dispose()
        dialect = postgresql.dialect()
        resource_ddl = str(CreateTable(VideoResource.__table__).compile(
            dialect=dialect
        ))
        user_ddl = str(CreateTable(WorkbenchUser.__table__).compile(
            dialect=dialect
        ))
        active_index = next(
            index
            for index in VideoRun.__table__.indexes
            if index.name == "ux_video_runs_active_name_owner"
        )
        active_index_ddl = str(CreateIndex(active_index).compile(dialect=dialect))
        self.assertIn("JSONB", resource_ddl)
        self.assertIn("password_hash", user_ddl)
        self.assertNotIn("password VARCHAR", user_ddl)
        self.assertIn("WHERE status = 'running'", active_index_ddl)

    def test_standalone_package_has_no_host_service_imports(self):
        package_root = Path(__file__).resolve().parents[1] / "qijia_video"
        source = "\n".join(
            path.read_text(encoding="utf-8")
            for path in package_root.rglob("*.py")
        )
        self.assertNotIn("from services", source)
        self.assertNotIn("import services", source)
        self.assertNotIn("PlatformTaskRepository", source)


if __name__ == "__main__":
    unittest.main()
