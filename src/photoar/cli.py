"""photoar 命令行入口。

    photoar build --photos <目录> --out <语料目录> [--arcoreimg <路径>] [--print-width-mm 152]
    photoar eval  --corpus <语料目录> [--samples 20] [--limit N] [--seed 1]

eval 的退出码：0 = 达到 spec §14.2 基线，1 = 未达标，2 = 用法或环境错误。
退出码可直接被 CI 使用。
"""

import argparse
import sys
from pathlib import Path

import cv2

from . import quality as Q
from .corpus import (
    DEFAULT_PRINT_WIDTH_M,
    IMAGE_SUFFIXES,
    CorpusIntegrityError,
    build_corpus,
    load_corpus,
)
from .evaluate import combine, evaluate

_DEFAULT_PRINT_WIDTH_MM = DEFAULT_PRINT_WIDTH_M * 1000.0


def _cmd_build(args: argparse.Namespace) -> int:
    photo_dir = Path(args.photos)
    paths = sorted(
        p for p in photo_dir.rglob("*") if p.suffix.lower() in IMAGE_SUFFIXES
    )
    if not paths:
        print(f"在 {photo_dir} 下没有找到图片（支持 {sorted(IMAGE_SUFFIXES)}）",
              file=sys.stderr)
        return 2
    if args.print_width_mm <= 0:
        print(f"--print-width-mm 必须为正数，收到 {args.print_width_mm!r}",
              file=sys.stderr)
        return 2

    try:
        entries = build_corpus(
            paths, args.out, seed=args.seed, arcoreimg=args.arcoreimg,
            print_width_m=args.print_width_mm / 1000.0,
        )
    except ValueError as exc:
        # build_corpus 在"一张都没入库成功"时抛这个——可能是文件本身读不出
        # 来、提取不到 ORB 特征点，或者（仅在提供 --arcoreimg 时）质量分低于
        # 阈值。这不是"未达标"（退出码 1 的语义），语料根本没建出来，是用法
        # /环境问题，归为退出码 2。
        print(
            f"入库失败：{exc}\n"
            f"常见原因：文件本身读不出来、图片提取不到 ORB 特征点，或者"
            f"（仅在提供 --arcoreimg 时）质量分低于 {Q.MIN_QUALITY_SCORE}。",
            file=sys.stderr,
        )
        return 2
    except Q.ArcoreimgMissing as exc:
        print(f"入库失败：{exc}", file=sys.stderr)
        return 2
    except RuntimeError as exc:
        # 覆盖 IncompleteWrite（DescStoreWriter 内部一致性被破坏，理论上不
        # 应触发）等其余 RuntimeError 子类。不捕获裸 Exception——真正的 bug
        # 应该继续以 traceback 形式暴露，而不是被这里悄悄转成退出码。
        print(f"入库失败：{exc}", file=sys.stderr)
        return 2

    print(f"入库 {len(entries)} 张，语料写入 {args.out}")
    if args.arcoreimg:
        sizes = [e.imgdb_bytes for e in entries if e.imgdb_bytes]
        if sizes:
            print(
                f".imgdb 体积  最小 {min(sizes)}  中位 {sorted(sizes)[len(sizes)//2]}  "
                f"最大 {max(sizes)} 字节"
            )
    return 0


def _cmd_eval(args: argparse.Namespace) -> int:
    if args.limit < 0:
        # M12：--limit -5 若不拦，entries[:-5] 会被 Python 切片语义悄悄解释
        # 成"从末尾截断"，而不是报错——用户很可能是打错了负号，实际评估的
        # 子集跟预期完全不同、还不会有任何提示。
        print(f"--limit 不能为负数，收到 {args.limit!r}", file=sys.stderr)
        return 2

    try:
        rec, entries = load_corpus(args.corpus)
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except (CorpusIntegrityError, ValueError, RuntimeError) as exc:
        # 语料本身顺序错位、被截断，或加载失败，不是用法错误，也不是识别率
        # 没达标——是环境/产物损坏，同样归为退出码 2，不能让调用方误以为是
        # "未达标"（1）。I4：这条 except 原来只捕获 CorpusIntegrityError；
        # desc.bin 被截断成"仍是 slot 步长整数倍，但数量与 manifest/index
        # 对不上"时，_verify_desc_fingerprints 会用一个越界 slot 下标去读
        # store，抛出未捕获的 IndexError，让进程以 Python 默认退出码 1
        # 结束。corpus.load_corpus 已经在指纹校验前加了三者数量的前置校验
        # （见 CorpusIntegrityError 的新增分支），这里再同 _cmd_build 一样
        # 宽泛捕获 ValueError/RuntimeError，兜住 DescStore/Vocab/InvertedIndex
        # 加载阶段其它形式的产物损坏。
        print(f"语料完整性校验失败：{exc}", file=sys.stderr)
        return 2

    chosen = entries[: args.limit] if args.limit else entries

    # C1：一次只解码一张参考图、调一次 evaluate()，而不是把 chosen 里的
    # 参考图全部 cv2.imread 进一个大 dict 再整体调用一次——后者在万张量级
    # 图库上会把全部解码后的参考图同时摊在内存里（12MP 手机照解码后约
    # 36.6MB/张，1 万张 ≈ 366GB），是 0d 第一次真实 eval 最先撞上的 OOM。
    # 这里用完一张的 img 就不再持有引用，任意时刻至多一张全分辨率参考图
    # 存活；每次 evaluate() 只收到单元素 refs 字典，结果最后用 combine()
    # 拼成一份聚合 Metrics（wave 2 / I8 可以把库外查询图的 evaluate() 结果
    # 也 combine() 进来，不需要改 evaluate() 本身的形状）。unreadable 计入
    # I7 的跳过原因统计，避免"评估参考图"这个分母悄悄变小却没人知道。
    per_ref_metrics = []
    unreadable = 0
    for e in chosen:
        img = cv2.imread(e.ref_path, cv2.IMREAD_COLOR)
        if img is None:
            unreadable += 1
            continue
        per_ref_metrics.append(
            evaluate(rec, {e.photo_id: img}, samples_per_ref=args.samples, seed=args.seed)
        )

    if not per_ref_metrics:
        print("参考图都读不出来，检查 manifest 里的 ref_path 是否还有效",
              file=sys.stderr)
        return 2

    metrics = combine(per_ref_metrics)
    print(f"图库规模    {len(entries)}")
    eval_line = f"评估参考图  {len(per_ref_metrics)}"
    if unreadable:
        eval_line += f"（另有 {unreadable} 张参考图读取失败，已跳过）"
    print(eval_line)
    print(metrics.as_report())
    return 0 if metrics.meets_baseline else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="photoar")
    sub = parser.add_subparsers(dest="cmd")

    b = sub.add_parser("build", help="从照片目录构建识别语料")
    b.add_argument("--photos", required=True)
    b.add_argument("--out", required=True)
    b.add_argument("--seed", type=int, default=0)
    b.add_argument("--arcoreimg", default=None,
                   help="arcoreimg 路径；省略则跳过质量分与 .imgdb 生成")
    b.add_argument(
        "--print-width-mm", type=float, default=_DEFAULT_PRINT_WIDTH_MM,
        help=f"参考图实际打印宽度（毫米），烘进 .imgdb；仅在提供 --arcoreimg 时"
             f"才会用到，默认 {_DEFAULT_PRINT_WIDTH_MM:g}mm",
    )
    b.set_defaults(func=_cmd_build)

    e = sub.add_parser("eval", help="用合成查询图评估识别率")
    e.add_argument("--corpus", required=True)
    e.add_argument("--samples", type=int, default=20)
    e.add_argument("--limit", type=int, default=0, help="只评估前 N 张，0 = 全部")
    e.add_argument("--seed", type=int, default=1)
    e.set_defaults(func=_cmd_eval)

    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_usage(sys.stderr)
        return 2
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
