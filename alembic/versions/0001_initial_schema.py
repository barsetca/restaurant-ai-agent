"""Create the M1 database schema.

Revision ID: 0001
Revises: None
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "menu_items",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_row_id", sa.String(255), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("ingredients", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("allergens", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("category", sa.String(120), nullable=False),
        sa.Column("price_minor", sa.Integer(), nullable=False),
        sa.Column("currency", sa.String(3), server_default="EUR", nullable=False),
        sa.Column("available", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.CheckConstraint("price_minor >= 0", name="ck_menu_items_price_nonnegative"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source_row_id"),
    )
    op.create_table(
        "restaurant_settings",
        sa.Column("key", sa.String(120), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("key"),
    )
    op.create_table(
        "orders",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("telegram_user_id", sa.BigInteger(), nullable=False),
        sa.Column("customer_name", sa.String(255), nullable=False),
        sa.Column("phone", sa.String(64), nullable=False),
        sa.Column("address", sa.Text(), nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("subtotal_minor", sa.Integer(), nullable=False),
        sa.Column("delivery_fee_minor", sa.Integer(), nullable=False),
        sa.Column("total_minor", sa.Integer(), nullable=False),
        sa.Column("currency", sa.String(3), server_default="EUR", nullable=False),
        sa.Column("status", sa.String(40), nullable=False),
        sa.Column("confirmation_key", sa.String(255), nullable=False),
        sa.Column("calendar_event_id", sa.String(255), nullable=True),
        sa.CheckConstraint("subtotal_minor >= 0", name="ck_orders_subtotal_nonnegative"),
        sa.CheckConstraint("delivery_fee_minor >= 0", name="ck_orders_delivery_fee_nonnegative"),
        sa.CheckConstraint("total_minor >= 0", name="ck_orders_total_nonnegative"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("confirmation_key"),
    )
    op.create_index("ix_orders_telegram_user_id", "orders", ["telegram_user_id"])
    op.create_index("ix_orders_status", "orders", ["status"])
    op.create_table(
        "order_items",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("order_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("menu_item_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("unit_price_minor", sa.Integer(), nullable=False),
        sa.Column("line_total_minor", sa.Integer(), nullable=False),
        sa.Column("name_snapshot", sa.String(255), nullable=False),
        sa.CheckConstraint("quantity > 0", name="ck_order_items_quantity_positive"),
        sa.CheckConstraint("unit_price_minor >= 0", name="ck_order_items_unit_price_nonnegative"),
        sa.CheckConstraint("line_total_minor >= 0", name="ck_order_items_line_total_nonnegative"),
        sa.ForeignKeyConstraint(["menu_item_id"], ["menu_items.id"]),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_order_items_order_id", "order_items", ["order_id"])
    op.create_index("ix_order_items_menu_item_id", "order_items", ["menu_item_id"])
    op.create_table(
        "conversation_state",
        sa.Column("thread_id", sa.String(255), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("thread_id"),
    )
    op.create_index("ix_conversation_state_user_id", "conversation_state", ["user_id"])
    op.create_table(
        "faq_sync",
        sa.Column("source_row_id", sa.String(255), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("indexed_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("source_row_id"),
    )
    op.create_table(
        "unanswered_questions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("sanitized_question", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("escalation_status", sa.String(40), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_unanswered_questions_created_at", "unanswered_questions", ["created_at"])
    op.create_table(
        "calendar_jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("order_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("status", sa.String(40), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.CheckConstraint("attempt_count >= 0", name="ck_calendar_jobs_attempt_count_nonnegative"),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_calendar_jobs_order_id", "calendar_jobs", ["order_id"])
    op.create_index("ix_calendar_jobs_status_next_attempt", "calendar_jobs", ["status", "next_attempt_at"])


def downgrade() -> None:
    op.drop_table("calendar_jobs")
    op.drop_table("unanswered_questions")
    op.drop_table("faq_sync")
    op.drop_table("conversation_state")
    op.drop_table("order_items")
    op.drop_table("orders")
    op.drop_table("restaurant_settings")
    op.drop_table("menu_items")
