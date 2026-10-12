# 项目结构与开发说明

项目仅支持 Windows，保留现有入口、配置文件和历史记录格式，按界面、流程、策略和存储划分职责。

## 入口与模块

| 文件 / 模块 | 职责 |
| --- | --- |
| `GUI.py` | 选择项目解释器并启动 GUI |
| `AutoCourse.py` | 命令行入口、运行环境与任务选择 |
| `modules/project_info.py` | 项目名称、开发者 GaryLu 与发布仓库 |
| `modules/gui_app.py` | Tk 界面、选择范围、表单与事件展示 |
| `modules/gui_support.py` | 配置保存、后台任务、取消与日志桥接 |
| `modules/chapter_batch.py` | 多章节回填顺序、单章失败隔离、取消与结果汇总 |
| `modules/chapter_tests.py` | 章节任务编排、网页读取与操作、题目核对、诊断 |
| `modules/chapter_verification.py` | 测试页人工安全验证等待、等待期限与核对重启上限 |
| `modules/chapter_records.py` | 题目指纹、历史复用、运行记录发现与旧记录导入 |
| `modules/answer_policy.py` | 覆盖开关、保留已有选择、逐题答案与置信度判断 |
| `modules/storage.py` | 严格 / 容错 JSON 读取、原子文本与 JSON 写入 |
| `modules/llm_types.py` | 模型配置、题目和答案契约、严格校验 |
| `modules/llm_relay.py` / `llm_adapters.py` | 模型请求、重试、输入输出格式适配 |
| `modules/llm.py` | 保持原 LLM 接口兼容 |
| `modules/course_urls.py` | 课程网址解析与统一规范化 |
| `modules/browser.py` / `login.py` | 浏览器会话及网站登录 |
| `modules/course_runner.py` / `course_playback.py` | 视频课程任务及播放流程 |
| `modules/runtime_env.py` / `installer.py` | 解释器选择与依赖安装 |

`chapter_records`、`answer_policy` 和 `storage` 不导入 Tk、Playwright 或模型客户端；查看本地历史、决定哪些答案可用时，无需初始化这些依赖。GUI 在启动批处理前固定目标和课程编号，后台服务只处理数据和事件。

## 章节处理顺序

1. 规范化课程网址，确认视频平台登录，再进入章节测试。
2. 读取题目；按指纹复用历史答案，或请求模型生成新答案。
3. 逐题判断是否可采用答案，缺失、无效及低置信度题分别记录跳过原因。
4. 重新核对全部题目，按覆盖策略回填或保留网页选择。
5. 暂存 / 交卷并核对网站确认，保存本地结果。

批量回填由 `chapter_batch.apply_review_batch` 执行。一章失败后记录失败并继续下一章；`CancelledError` 直接向上传播，停止后不启动下一章。完成统计区分已保存 / 已交卷、失败和已交卷 / 无可用答案的跳过项。测试页安全验证先暂停等待；验证超时、网页内容变化或无法确认提交结果时，停止当前章节的操作。

`ChapterReviewBrowser` 延迟启动并持有批次共用的 Chrome、登录上下文和页面保护；逐章关闭测试及 SSO 中转标签，保留课程根页，整个批次结束或取消时统一释放。已交卷或本地数据不完整的记录不触发浏览器启动。浏览器连接断开后，仅为下一章节重建会话，不重发上一章的保存或交卷。

课程目录缓存只属于当前浏览器会话，绑定课程网址、页面对象、当前地址和章节名称列表。复用前检查页面仍然可用、没有登录表单或安全验证、目录未变化；换课或登录失效走原有认证流程，章节列表变化要求重新读取。此缓存不代表网站已经保存或交卷，模型答案复用及提交确认规则分别处理。

测试页安全验证由 `chapter_verification.wait_for_chapter_verification` 保留当前页面等待用户手动完成，等待上限为 120 秒，取消直接向上传播，不发送作答请求。读取或核对过程中出现验证后从第一题重新检查，最多重新开始三次；回填和提交前若出现验证，还须核对整章选中项与期望一致。等待期限与重启上限集中定义于该模块，不属于 `config` 的配置项。GUI 的导航事件显示“等待验证”和“重新核对”，失败诊断区分验证超时和题目/答案变化。

## 配置与记录

- `config.example` 是 Windows 配置模板，实际配置保存在本地 `config`。GUI 保存时重新读取磁盘，仅更新管理的字段，保留其他字段。
- `.autovisor/chapter-tests/<运行>/course-N/test-N/` 保存 `context.json`、`questions.json`、`review.json`、`result.json` 和截图。旧目录及记录无需迁移。
- 项目改名后仍使用原历史目录，以保留记录中的截图路径。环境变量使用 `AUTOCOURSE_LLM_URL/KEY/MODEL`，同时兼容旧前缀；新名称优先。
- `read_json` 用于必须有效的业务数据，异常由业务层处理；`read_json_safely` 用于允许缺失的显示元数据和历史查询，不把损坏的数据静默用于回填。
- 配置、章节 JSON、GUI 本地修改和 Cookies 共用原子写入：同目录临时文件写完并关闭后，通过 `os.replace` 替换目标。序列化、写入或替换失败时保留旧文件，清理临时文件。父目录由业务层创建。
- 原子写入保证单个文件完整，不提供多个文件的事务或多进程编辑合并。请避免多个实例同时修改同一份记录。

兼容导入仍保留：原先从 `chapter_tests` 导入的记录 / 策略辅助函数以及从 `gui_support` 导入的历史查询函数继续有效。新增代码应直接依赖职责对应的模块，避免界面中的历史查询依赖网页操作模块。

人工验证等待函数仍可从 `chapter_tests` 导入；新的调用方可直接使用 `chapter_verification`，避免等待策略继续扩充章节操作模块。

## 测试

使用项目 `.venv` 中的 Python，在项目根目录执行：

```powershell
& .venv/Scripts/python.exe -X utf8 -m unittest discover -s tests
```

只验证存储、批处理与会话生命周期：

```powershell
& .venv/Scripts/python.exe -X utf8 -m unittest tests.test_storage tests.test_chapter_batch tests.test_chapter_sessions
```

测试使用临时目录、模拟模型响应及本地路由的网页样例，不请求配置的模型接口或提交真实课程。章节浏览器测试需要安装 Chrome；Windows 会跳过 POSIX 文件权限检查。可选浏览器回归测试是否启用，由 `AUTOCOURSE_BROWSER_TESTS` 控制。

修改网页导航时运行章节 / 登录测试；修改模型格式时运行 LLM / 中继测试；修改 GUI 时运行 GUI 回归。存储测试覆盖磁盘刷新失败、目标文件被占用、配置保存失败、严格读取与容错读取的区别。

模型服务商兼容回归可运行 `tests.test_llm_providers`、`tests.test_llm_relay`、`tests.test_llm`。服务商域名自动识别、DeepSeek JSON 模式转换与硅基流动 InternVL 图文顺序集中在中继适配器；基础地址补齐由 `LLMSettings.endpoint` 负责。详见 [模型接入与中继](llm-relay.md)。

使用说明见 [章节测试指南](chapter-tests.md)。

## 首次安装

GitHub 用户下载并解压源码后，先安装支持的 Windows Python 和系统 Chrome，再按 README 创建 `.venv`、执行 `pip install ".[gui,captcha]"` 并启动 `GUI.py`。该组合包含 GUI 与自动滑块依赖，与 Windows CI 的两个 extra 一致；不开启自动滑块时可只安装 GUI extra。现有环境不需要在每次启动时重新安装。

## 许可文件与分发

项目新增代码采用 [MIT 许可](../LICENSE)，署名 GaryLu。上游原始版权、MIT 许可及来源说明保存在 [NOTICE](../NOTICE)。`pyproject.toml` 使用 SPDX 标识 `MIT` 并声明许可文件，构建后端要求 `setuptools>=77` 以支持这些元数据字段。

源码分发及 `build.py` 的命令行产物携带 `LICENSE` 和 `NOTICE`。添加其他发行方式时也应携带相应声明；第三方组件的许可文件不能用项目 MIT 许可替代。

## 工作区清理

`GUI.py`、`AutoCourse.py` 和 `build.py` 是有效入口，测试与网页样例也属于项目源码，应保留。`build/`、`*.egg-info`、`__pycache__` 和一次性检查脚本属于可重新生成的开发产物，不应随源码发布。

实际配置、Cookies、答案与截图记录、诊断数据、运行日志及备用依赖属于本地数据，应由用户按需要管理；不要直接删除整个 `.autovisor`、`data` 或 `packages` 目录。

## Git 管理与后续推送

源码仓库应包含运行、开发和复现所需内容；个人配置与业务记录保留在本地。

| 内容 | 是否纳入 Git | 原因 |
|---|---|---|
| `AutoCourse.py`、`GUI.py`、`modules/` | 是 | 应用入口与源码 |
| `tests/` 及 `tests/fixtures/` | 是 | 回归测试和自制网页样例 |
| `README.md`、`docs/` 中的公开文档 | 是 | 用户说明、结构与接口约定；内部阶段记录留在本地 |
| `pyproject.toml`、`uv.lock`、`.python-version` | 是 | 依赖声明、解析结果与解释器版本；锁文件不是运行缓存 |
| `config.example` | 是 | 不含账号、Key 或真实课程链接的配置模板 |
| `build.py`、`.github/` | 是 | 构建、启动和持续检查流程 |
| `LICENSE`、`NOTICE` | 是 | 项目及随附上游组件的版权和许可 |
| `.gitignore`、`.gitattributes` | 是 | 跟踪边界与跨平台换行规则 |
| `data/mirrors.json` | 是 | 公共依赖镜像配置，程序运行时读取 |
| `resources/stealth.min.js`、`resources/zhs.ico` | 是 | 现有运行脚本及打包图标，保留来源和许可说明 |
| `config`、配置备份、`.env`、Cookies | 否 | 本地账号、Key、课程与登录信息 |
| `.autovisor/`、`.autocourse/`、`logs/`、`captcha/`、`preview/` | 否 | 学习记录、模型答案、截图、浏览器资料与诊断 |
| `.venv/`、`packages/`、`build/`、`dist/`、Python 缓存 | 否 | 可安装或生成的环境和产物，发行包可单独作为 Release 附件 |
| 编辑器配置、操作系统临时文件 | 否 | 与源码运行和复现无关的本机状态 |

`data`、`resources` 采用允许列表，不能直接把整个目录加入仓库。新增公共资源时应同步调整 `.gitignore`；测试用图片可放在 `tests/fixtures` 并检查不含真实课程或个人信息。`.gitattributes` 统一源码为 LF，并将图像标为二进制。

默认发布分支为 `main`。Windows 工作流支持 `main` 和 `master` 的推送、拉取请求及手动触发，检查 Python 3.10–3.13。

发布前应检查 Git 候选文件、配置模板和待提交差异中的凭据；测试夹具只使用模拟值。`.gitignore` 不会移除已经跟踪的文件；后续若误跟踪个人数据，必须处理索引及可能包含它的历史，不能只补忽略规则。

后续本地复核可使用：

```powershell
git ls-files --others --exclude-standard
git ls-files config data/cookies.json .autovisor logs packages .venv
git diff --cached --stat
git diff --cached --check
```

第二条命令在正确跟踪边界下应无输出。提交源码时保留上述公共资源与许可文件，不强制加入被忽略的目录。

浏览器和命令行入口从 `resources/stealth.min.js` 加载随附脚本。源码仓库及发行包均需保留该相对路径。
