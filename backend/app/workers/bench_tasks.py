"""模型对比台的后台任务（import 即注册）。单独成文件，不动主任务模块。"""
from __future__ import annotations

from ..queue import JobContext, handler
from ..services import bench as bench_svc


@handler("bench_image_one")
def h_bench_image_one(ctx: JobContext) -> None:
    ctx.progress(10, "出图中")
    bench_svc.run_image_one(ctx.db, ctx.job().ref_id)


@handler("bench_lyric_one")
def h_bench_lyric_one(ctx: JobContext) -> None:
    ctx.progress(10, "写词中")
    bench_svc.run_lyric_one(ctx.db, ctx.job().ref_id)
