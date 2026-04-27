import asyncio
import signal

import org


async def run_worker() -> None:
    await org.db.connect()
    tasks: list[asyncio.Task] = []
    try:
        if org.ORG_ENABLE_SAMPLE_DATA:
            await org.create_sample_data()

        if not org.ORG_ENABLE_BACKGROUND_JOBS:
            org.logger.info("Worker started with ORG_ENABLE_BACKGROUND_JOBS=false; idling")
            while True:
                await asyncio.sleep(60)

        while not org._try_acquire_worker_lock():
            await asyncio.sleep(10)

        org.logger.info("Dedicated worker acquired lock and started jobs")
        tasks = [
            asyncio.create_task(org.update_stock_prices()),
            asyncio.create_task(org.check_and_process_proposals()),
        ]
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await org.db.disconnect()


async def _main() -> None:
    stop = asyncio.Event()

    def _request_stop() -> None:
        stop.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _request_stop)
        except NotImplementedError:
            pass

    worker_task = asyncio.create_task(run_worker())
    done, _ = await asyncio.wait({worker_task, asyncio.create_task(stop.wait())}, return_when=asyncio.FIRST_COMPLETED)
    if stop.is_set() and not worker_task.done():
        worker_task.cancel()
    for task in done:
        if task is worker_task:
            exc = task.exception()
            if exc:
                raise exc


if __name__ == "__main__":
    asyncio.run(_main())
