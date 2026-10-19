"""Explicit saver initialization and terminal checkpoint retention, never startup DDL."""
import argparse
import asyncio

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from redis.asyncio import Redis

from app.db.session import create_database_engine
from app.workflow.checkpoint import open_checkpointer
from app.workflow.repository import WorkflowRepository
from app.workflow.session_commit import receipt_key


class CheckpointSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", hide_input_in_errors=True)
    checkpoint_database_url: SecretStr
    database_url: SecretStr = SecretStr("")
    redis_url: SecretStr = SecretStr("redis://127.0.0.1:6379/0")
    workflow_retention_days: int = Field(default=7, ge=1, le=90)


async def run(command, settings):
    async with open_checkpointer(settings.checkpoint_database_url.get_secret_value()) as saver:
        if command == "setup":
            await saver.setup()
            return
        engine = create_database_engine(settings.database_url.get_secret_value())
        redis = Redis.from_url(settings.redis_url.get_secret_value(), socket_timeout=2, socket_connect_timeout=2)
        try:
            repository = WorkflowRepository(engine)
            for workflow_id in await repository.expired_completed(settings.workflow_retention_days):
                wid = str(workflow_id)
                async with repository.lock(wid):
                    row = await repository.get(wid)
                    if row["status"] != "completed":
                        continue
                    await saver.adelete_thread(wid)
                    if row["session_status"] == "committed":
                        await redis.delete(receipt_key(wid))
                    await repository.update(wid, checkpoint_pruned=True)
        finally:
            await redis.aclose()
            await engine.dispose()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("setup", "prune"))
    args = parser.parse_args()
    try:
        asyncio.run(run(args.command, CheckpointSettings()))
    except Exception:
        # Never print psycopg errors containing credentials/connection strings.
        parser.exit(1, "Checkpoint maintenance failed; check configuration and database availability.\n")
    print("Checkpoint maintenance completed.")


if __name__ == "__main__":
    main()
