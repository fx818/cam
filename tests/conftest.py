import os

TEST_DATABASE_URL = os.environ.get(
    "CAM_TEST_DATABASE_URL", "postgresql+asyncpg://cam:cam@localhost:5432/cam_test"
)

os.environ["CAM_DATABASE_URL"] = TEST_DATABASE_URL
os.environ["CAM_ENV"] = "dev"
