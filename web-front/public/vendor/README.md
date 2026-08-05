# vendor/

一个第三方二进制：`opencv.js`。这里记清它是什么、为什么必须是它、以及换版本要重做哪一步。

## opencv.js

| | |
|---|---|
| 来源 | `https://cdn.jsdelivr.net/npm/@techstark/opencv-js@5.0.0-release.1/dist/opencv.js` |
| 版本 | **5.0.0-release.1**（OpenCV 5.0.0 的 emscripten 构建） |
| 大小 | 13,298,869 B（单文件，wasm 以 base64 内联） |
| sha256 | `b873c8211421da7b9bf41ae157a923f05a46a0b8d3e5904c44c6f3ad6d39a1bd`（见 `sha256.txt`） |
| 许可 | Apache-2.0（OpenCV 4.5.0 起） |

> ⚠️ **`opencv.orig.js` 不在版本库里**（`.gitignore` 挡着）。它只在重跑
> `npm run wasm:split` 时才用得到 —— 那个脚本要从里面把内联的 wasm 抽出来。
> 需要它就照着上面那个 URL 取回来，并核对 sha256：
>
> ```bash
> curl -fsSL https://cdn.jsdelivr.net/npm/@techstark/opencv-js@5.0.0-release.1/dist/opencv.js \
>   -o public/vendor/opencv.orig.js
> sha256sum -c public/vendor/sha256.txt   # 那个文件记的就是它的哈希
> ```
>
> 仓库里提交的是**产物**：`opencv.js`（128KB，patch 过）、`opencv.wasm`（11.4MB）、
> 以及各自预压的 `.br`。部署机上四个都要有，一个字节也不用现算。

### 为什么必须 vendor 一份 OpenCV，而不是自己写 ORB

查询侧的描述子必须和**入库侧**落在同一个特征空间里 —— 库里 `desc.bin` 存的是服务端
`cv2.ORB` 算出来的 256bit rBRIEF。自己重写一份 ORB 要逐位复现 FAST-9 阈值与 NMS、
Harris 响应排序、8 层金字塔、intensity-centroid 方向、每层 7×7 σ=2 的高斯模糊、
以及那 512 个硬编码的 rBRIEF 采样点对。任何一处不一致的后果是**描述子静默失配**：
不报错，只是识别率归零。

用同一份 C++ 代码编译出来的 wasm，一致性才有来源可查 —— 而且它是可验证的，见
`test/golden/`：Python 侧和浏览器侧对**同一段原始灰度字节**各提一次，逐位比对。

### 为什么是 5.0.0 而不是 4.x

两个理由叠起来，都是硬的：

1. **服务端跑的是 `cv2 5.0.0`**（本机实测；镜像里 `opencv-python-headless>=4.9` 解出来
   的也是 5.0.x）。特征空间要对齐，版本对齐是最省事的一条。
2. **`@techstark/opencv-js@4.11.0-release.1` 里根本没有 features2d/calib3d** ——
   实测那份文件里 `ORB` / `detectAndCompute` / `BFMatcher` / `findHomography` /
   `KeyPointVector` 一个符号都搜不到。也就是说 4.x 那条路不是"版本旧一点"，是**不存在**。

### 换版本要做什么

1. 换文件、更新上面那张表和 `sha256.txt`；
2. **重跑 `test/golden/`**。这一步不是形式：换 OpenCV 版本等于换特征空间，
   golden 一红就说明全库 `desc.bin` 对新版本作废，必须整库重建。
   而它不红的话，这一次升级是安全的 —— 这正是那套 golden 存在的理由。

### 体积：知道它重，也知道怎么减

13.3MB 单文件（gzip 后约 3.5MB，brotli 约 2.8MB），因为 wasm 是 base64 内联的。
浏览器只下一次（`Cache-Control: immutable` + Cache Storage），但首屏确实要等。

两条能减的路，都**没有在这一版做**，理由是它们都要引入构建步骤，而现在这个仓库是
零构建的：

- 分离 `.wasm`（去掉 base64 的 4/3 膨胀，约省 3MB）—— 需要 `@techstark` 提供分离版本
  或自己用 emsdk 出包；
- 自定义构建裁剪到 `core` + `imgproc` + `features2d` + `calib3d`（估计能到 2–3MB）——
  需要 emscripten 工具链（`docker run emscripten/emsdk` + `opencv/platforms/js/build_js.py`）。
  本机没有 `emcc`，所以这一步只能在有工具链的机器上做。
