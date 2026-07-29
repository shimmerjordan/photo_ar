"""photoar 命令行入口。

    photoar build --photos <目录> --out <语料目录> [--arcoreimg <路径>]
                  [--print-width-mm 152] [--holdout-frac 0.1]
    photoar eval  --corpus <语料目录> [--samples 20] [--limit N] [--seed 1]
                  [--strict-latency]

--limit N：库内参考图与库外留出图**各等间距抽样** N 张（见 `_strided`），
图库规模不变。

eval 的退出码：0 = 达到 spec §14.2 基线，1 = 未达标，2 = 用法或环境错误。
退出码可直接被 CI 使用。

eval 的进度行（`[eval] 库内参考图 i/n ...`）走 **stderr**，stdout 只有那份
会被引用进结果文档的报告。上规模跑动辄一小时，没有进度行就无法判断活性。

--holdout-frac（finding I8）：build 时按这个比例留出一部分照片彻底不
入库，写进语料目录的 holdout.json；随后 `photoar eval` 会自动读取它，
把这些照片当"库外查询"（真实场景里最常见的假阳性来源：用户拍了一张库
里没有的东西），在报告里单独给出库外误识别率，不与库内数字混在一起。
"""

import argparse
import sys
import time
from pathlib import Path

import cv2

from . import quality as Q
from .corpus import (
    DEFAULT_PRINT_WIDTH_M,
    IMAGE_SUFFIXES,
    CorpusIntegrityError,
    build_corpus,
    load_corpus,
    load_holdout,
    select_holdout,
    write_holdout,
)
from .evaluate import combine, combine_out_of_library, evaluate, evaluate_out_of_library

_DEFAULT_PRINT_WIDTH_MM = DEFAULT_PRINT_WIDTH_M * 1000.0


def _cmd_build(args: argparse.Namespace) -> int:
    photo_dir = Path(args.photos)
    all_paths = sorted(
        p for p in photo_dir.rglob("*") if p.suffix.lower() in IMAGE_SUFFIXES
    )
    if not all_paths:
        print(f"在 {photo_dir} 下没有找到图片（支持 {sorted(IMAGE_SUFFIXES)}）",
              file=sys.stderr)
        return 2
    if args.print_width_mm <= 0:
        print(f"--print-width-mm 必须为正数，收到 {args.print_width_mm!r}",
              file=sys.stderr)
        return 2

    # finding I8：--holdout-frac 从全部照片里确定性地切出一部分，彻底不参与
    # 下面的 build_corpus——这部分留出图会被写进语料目录的 holdout.json，
    # 供 `photoar eval` 当"库外查询"测出真正的生产环境假阳性率（用户拍了
    # 一张库里没有的东西），而不是像此前那样只能测"库内 A 认成库内 B"。
    # 默认 0.0 不留出任何图，行为与这个特性存在之前完全一致。
    library_paths, holdout_paths = all_paths, []
    if args.holdout_frac:
        try:
            library_paths, holdout_paths = select_holdout(
                all_paths, args.holdout_frac, args.seed
            )
        except ValueError as exc:
            print(f"--holdout-frac 参数有问题：{exc}", file=sys.stderr)
            return 2

    try:
        entries = build_corpus(
            library_paths, args.out, seed=args.seed, arcoreimg=args.arcoreimg,
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
    if holdout_paths:
        write_holdout(args.out, holdout_paths)
        print(
            f"留出 {len(holdout_paths)} 张作为库外查询测试集（从未入库），"
            f"写入 {args.out}/holdout.json —— `photoar eval` 会自动用它们"
            f"测库外误识别率"
        )
    return 0


def _strided(items: list, limit: int) -> list:
    """按**等间距**抽 limit 个元素，而不是取前 limit 个。

    `--limit` 的用途是在上万张语料上把 eval 的墙上时钟压回可接受范围
    （实测 0.1-0.3 s/查询），但"取前 N 个"会让覆盖面完全由文件名排序决定。
    Oxford5k 上实测过这个偏差有多严重：manifest 按路径排序，前 500 张只
    落在 ashmolean/balliol/all_souls/bodleian 四个分组里，而语料里最大也
    最自相似的 oxford(1502)/magdalen(685)/christ_church(543) 三组一张都
    没覆盖到——那样量出来的是"四个地标的识别率"，不是这份语料的识别率，
    而结果文档里只会写着"评估了 500 张"。

    等间距抽样是确定性的（不需要 seed，同一份 manifest 必得同一个子集），
    且按各分组的原始占比铺开。注意它只减少**被当作查询源的参考图**，图库
    规模不变——粗排仍在全库竞争，所以限幅不会让识别任务变简单。
    """
    n = len(items)
    if not limit or limit >= n:
        return items
    step = n / limit
    # step >= 1，所以 int(i * step) 严格递增，不会取到重复元素。
    return [items[int(i * step)] for i in range(limit)]


_PROGRESS_LINES = 20


def _progress_every(n: int) -> int:
    """进度行的打印间隔：整段大约打 `_PROGRESS_LINES` 行。

    按**张数**而不是按时间（"每 30 秒一行"）触发，这样同一份语料 + 同样的
    参数，进度行出现的位置逐次运行完全一致——行里的耗时数字当然会变，但
    "第几张打一行"是确定的，测试才能精确断言而不用做模糊匹配。
    """
    return max(1, n // _PROGRESS_LINES)


def _progress(label: str, i: int, n: int, t0: float, every: int, tag: str = "eval") -> None:
    """往 stderr 打一行 `i/n + 已用时间 + 预计剩余`。

    tag 是行首那个方括号里的东西。默认 "eval"，`bench/threshold_scan.py`
    复用这个函数时传自己的 tag——否则它的进度行会自称 `[eval]`，跟 eval 的
    日志混在一起时分不清是哪次跑。

    为什么必须有：0d 上规模的一次 eval 是 29740 次查询、约 1 小时，而在这
    之前整个过程零输出——日志文件一小时都停在 0 字节，从外面完全分不清它
    是在正常跑、卡在某张图上、还是早就死了。长跑必须能被判断活性，否则
    只能靠 `ps` 猜。

    打在 stderr 而不是 stdout：stdout 那份报告会被逐行引用进结果文档，
    掺进进度行就污染了它（库外误识别详情走 stderr 是同一个理由）。

    预计剩余用的是**至今为止的平均速度**，不是瞬时速度。参考图之间的成本
    差异不小（分辨率、特征点数都不同），瞬时速度会剧烈跳动；平均值在前几
    张会偏，但越跑越准，对"还要等多久"这个用途够了。
    """
    if i % every and i != n:
        return
    elapsed = time.time() - t0
    eta = elapsed / i * (n - i) if i else 0.0
    print(
        f"[{tag}] {label} {i}/{n}  已用 {elapsed / 60:.1f}min  "
        f"预计还需 {eta / 60:.1f}min",
        file=sys.stderr,
        flush=True,
    )


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

    chosen = _strided(entries, args.limit)

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
    t0 = time.time()
    ref_every = _progress_every(len(chosen))
    for i, e in enumerate(chosen, 1):
        img = cv2.imread(e.ref_path, cv2.IMREAD_COLOR)
        if img is None:
            unreadable += 1
        else:
            per_ref_metrics.append(
                evaluate(rec, {e.photo_id: img}, samples_per_ref=args.samples, seed=args.seed)
            )
        # 读不出来的那张也要计进进度：用 if/else 而不是 `continue`，否则读取
        # 失败会让计数跳号，看日志的人会以为漏打了。
        _progress("库内参考图", i, len(chosen), t0, ref_every)

    if not per_ref_metrics:
        print("参考图都读不出来，检查 manifest 里的 ref_path 是否还有效",
              file=sys.stderr)
        return 2

    # finding I8：如果 build 时给了 --holdout-frac，语料目录里会有
    # holdout.json——这些照片从未进入语料，是真正意义上的"库外查询"（不是
    # "库内某张没被抽到当 ref"）。用它们当合成查询源，测的就是生产环境里
    # 最常见的那种假阳性：用户拍了一张库里没有的东西。跟库内参考图（C1）
    # 同样的理由，一次只解码一张、调一次 evaluate_out_of_library()，不把
    # 留出图也整批摊进内存。
    # --limit 同样约束库外这一层：原来它只截断库内参考图，留出集是**无上限**
    # 遍历的。1 万张语料 + --holdout-frac 0.1 = 1000 张留出，配 --samples 10
    # 就是 1 万次库外查询，即使 --limit 100 也照样跑满——限幅形同失效，而
    # 用户以为自己已经限住了。
    all_holdout = load_holdout(args.corpus)
    n_holdout_total = len(all_holdout)
    holdout_paths = _strided(all_holdout, args.limit)
    per_oos_metrics = []
    oos_unreadable = 0
    oos_t0 = time.time()
    oos_every = _progress_every(len(holdout_paths))
    for i, p in enumerate(holdout_paths, 1):
        img = cv2.imread(str(p), cv2.IMREAD_COLOR)
        if img is None:
            oos_unreadable += 1
        else:
            per_oos_metrics.append(
                evaluate_out_of_library(
                    rec, {str(p): img}, samples_per_ref=args.samples, seed=args.seed
                )
            )
        _progress("库外查询图", i, len(holdout_paths), oos_t0, oos_every)

    if holdout_paths and not per_oos_metrics:
        print("库外查询图都读不出来，检查 holdout.json 里的路径是否还有效",
              file=sys.stderr)
        return 2

    oos_metrics = combine_out_of_library(per_oos_metrics) if holdout_paths else None

    # Minor #10：P95 延迟默认不影响退出码（0a 的暴力检索 P95 534ms 远超
    # 80ms 目标，但已记录的结论是"达标"——默认折进判定会追溯改写那条历史
    # 结论）。--strict-latency 是显式 opt-in，只有调用方主动要求才会让超
    # 延迟的结果把退出码从 0 翻成 1。
    metrics = combine(per_ref_metrics, oos=oos_metrics, latency_gate=args.strict_latency)
    print(f"图库规模    {len(entries)}")
    # 覆盖面必须自带分母：限幅跑出来的数字如果只写"评估参考图 500"，读者没法
    # 分辨这是全量还是 4500 张里的 500 张，而结果文档要求写明覆盖了多少张。
    eval_line = f"评估参考图  {len(per_ref_metrics)}/{len(entries)}"
    if args.limit:
        eval_line += f"（--limit {args.limit}，等间距抽样）"
    if unreadable:
        eval_line += f"（另有 {unreadable} 张参考图读取失败，已跳过）"
    print(eval_line)
    if holdout_paths:
        oos_line = f"库外查询图  {len(per_oos_metrics)}/{n_holdout_total}"
        if args.limit:
            oos_line += f"（--limit {args.limit}，等间距抽样）"
        if oos_unreadable:
            oos_line += f"（另有 {oos_unreadable} 张读取失败，已跳过）"
        print(oos_line)
    print(metrics.as_report())
    if oos_metrics is not None and oos_metrics.false_positive_matches:
        # 本轮修复（select_holdout 按内容哈希整组去留）追加：内容哈希去重
        # 只堵住了字节完全相同的重复跨边界，堵不住"重新编码的近似重复"
        # （同一张照片被压缩/裁切/转码成不同字节，哈希本身就不相等）。这里
        # 不猜哪些是真误识别、哪些是数据卫生问题，只是把每次库外误识别
        # 命中的库内 photo_id 报出来，供验收跑之后人工/脚本核对该 photo_id
        # 在 manifest 里的 ref_path 是否其实是这张留出图的另一份编码。
        print(
            "库外误识别命中详情（qid=留出图路径 -> 命中的库内 photo_id；"
            "供核对是否为重新编码的近似重复，而非真实误识别）：",
            file=sys.stderr,
        )
        for qid, photo_id in oos_metrics.false_positive_matches:
            print(f"  {qid} -> {photo_id}", file=sys.stderr)
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
    b.add_argument(
        "--holdout-frac", type=float, default=0.0,
        help="按这个比例（0~1，不含两端）从照片里确定性留出一部分，彻底不"
             "入库，写入语料目录的 holdout.json；`photoar eval` 会自动用它们"
             "测库外误识别率（finding I8）。默认 0 = 不留出，行为不变",
    )
    b.set_defaults(func=_cmd_build)

    e = sub.add_parser("eval", help="用合成查询图评估识别率")
    e.add_argument("--corpus", required=True)
    e.add_argument("--samples", type=int, default=20)
    e.add_argument(
        "--limit", type=int, default=0,
        help="库内参考图与库外留出图各**等间距抽样** N 张来评估（不是取前 N 张，"
             "否则覆盖面由文件名排序决定），0 = 全部。图库规模不变，粗排仍在"
             "全库竞争",
    )
    e.add_argument("--seed", type=int, default=1)
    e.add_argument(
        "--strict-latency", action="store_true",
        help="把 P95 延迟也折进达标判定（默认不折入，因为这会改变已录得的"
             "0a/0b 历史结论的口径——0a 的暴力检索 P95 就超出目标，见 Minor #10）",
    )
    e.set_defaults(func=_cmd_eval)

    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_usage(sys.stderr)
        return 2
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
