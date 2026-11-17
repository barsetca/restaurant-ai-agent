import os

os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+asyncpg://restaurant:synthetic_test_password@localhost:5432/restaurant_test",
)
