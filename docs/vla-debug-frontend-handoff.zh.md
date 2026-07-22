# VLA 模型调试页前端交接文档

本文面向前端实现同学，说明 `nexus-api` 已提供的 VLA 模型调试能力、页面应该怎么做、接口怎么调用，以及哪些地方第一版不要做复杂。

## 1. 页面定位

页面入口建议命名为：`VLA 模型调试`。

这个页面给算法工程师使用，用来在真实机器人上调试已经启动好的 VLA Policy Service。它不是普通用户入口，也不走 LLM 意图识别。

核心链路是：

```text
浏览器
  -> nexus-api
  -> 在线的 nexus-edge
  -> edge 拉 ontology-core observation
  -> edge 调 VLA Policy Service
  -> edge 把 action chunk 发给 ontology-core 执行
```

浏览器只访问 `nexus-api`，不要直连 Edge，也不要直连 VLA 模型服务。

## 2. 核心概念

- `Robot Type`：机器人类型，例如 `W1`、`CR5`。VLA 模型服务按机器人类型适配。
- `Machine`：具体一台机器，也就是一台在线 Edge/机器人实例。
- `VLA Policy Service`：算法工程师手动启动的模型服务地址，例如 `10.18.103.121:3000`。
- `Instruction`：本次要测试的自然语言指令，例如 `pick up the cup`。
- `Instruction History`：历史输入记录，只用于快速再次填入输入框，不是实验记录。
- `Active VLA Task`：某台 Machine 当前正在运行的 VLA 调试任务。

当前约束：

- 一个 `Robot Type` 可以有多台 `Machine`。
- 一台 `Machine` 同一时间只能运行一个 VLA 调试任务。
- 运行中不能切换 Machine 或 VLA Policy Service。
- VLA Policy Service 当前由算法工程师手动启动，页面只负责登记 endpoint，不负责启动/停止模型服务。
- 当前机器在线状态上报还未接入本服务，第一版不因为 `is_online = false` 阻止启动。

## 3. 推荐页面结构

页面可以做成一个单页工具，不需要复杂工作台。

建议分区：

- 顶部选择区：`Machine`、`VLA Policy Service`。Machine 列表中展示 `robot_type` 标签。
- Policy Service 管理区：新增/编辑模型服务地址。
- 相机画面区：显示当前 Machine 的 VLA 模型输入图像，多路图像按 view 展示。
- 指令控制区：instruction 输入框、历史指令、Start、Stop、Reset。
- 状态区：机器在线状态、当前任务、最近错误。

第一版建议保持朴素：能选机器、填服务、看图、发指令、停、复位即可。

## 4. 页面初始化流程

### 4.1 查询全部机器

```http
GET /api/vla-debug/machines
```

响应示例：

```json
{
  "code": 200,
  "msg": "success",
  "data": {
    "items": [
      {
        "machine_id": "w1-01",
        "name": "W1 Lab 01",
        "type_name": "W1",
        "is_online": true,
        "robot_type": "W1",
        "can_start_vla": true
      },
      {
        "machine_id": "cr5-01",
        "name": "CR5 Lab 01",
        "type_name": "CR5",
        "is_online": false,
        "robot_type": "CR5",
        "can_start_vla": true
      }
    ]
  }
}
```

前端使用：

- 直接展示 Machine 列表，机器名建议用 `name || machine_id`。
- 在每个 Machine 项上展示 `robot_type` 作为标签，例如 `W1`、`CR5`。
- `is_online` 当前只展示，不阻止用户 Start。
- Start 按钮可以依赖 `can_start_vla === true`，但当前后端会对同类型机器都返回 true。
- 选中 Machine 后，前端从该项读取 `robot_type`，用于查询 Policy Service、历史指令和 Start 请求。

### 4.2 可选：查询机器人类型和类型下机器

领域设计仍保留 `Robot Type -> Machine` 二级关系，后续同类型多机器变多后可以再切回二级筛选。当前页面展示建议先使用 `GET /api/vla-debug/machines` 合并列表。

查询机器人类型：

```http
GET /api/vla-debug/robot-types
```

查询某类型下机器：

```http
GET /api/vla-debug/robot-types/{robot_type}/machines
```

示例：

```http
GET /api/vla-debug/robot-types/W1/machines
```

响应示例：

```json
{
  "code": 200,
  "msg": "success",
  "data": {
    "items": [
      {
        "machine_id": "w1-01",
        "name": "W1 Lab 01",
        "type_name": "W1",
        "is_online": true,
        "robot_type": "W1",
        "can_start_vla": true
      },
      {
        "machine_id": "w1-02",
        "name": "W1 Lab 02",
        "type_name": "W1",
        "is_online": false,
        "robot_type": "W1",
        "can_start_vla": true
      }
    ]
  }
}
```

前端使用：

- 这两个接口当前作为可选筛选能力保留。
- 如果 Edge 实际未连接，Start 请求会在后端下发阶段失败，前端按错误提示展示即可。

### 4.3 同时查询 Policy Service

```http
GET /api/vla-debug/robot-types/{robot_type}/policy-services
```

响应示例：

```json
{
  "code": 200,
  "msg": "success",
  "data": {
    "items": [
      {
        "service_id": "7fd5b8f0f5e14fd9a2eab8",
        "robot_type": "W1",
        "name": "w1-pick-cup-0702",
        "endpoint": "10.18.103.121:3000",
        "created_at": "2026-07-02T08:30:00+00:00",
        "updated_at": "2026-07-02T08:30:00+00:00",
        "last_used_at": null
      }
    ]
  }
}
```

前端使用：

- 下拉展示 `name`，副文本展示 `endpoint`。
- 运行中不能切换当前选中的 service。
- `protocol` 是后端内部默认值，前端不用展示，也不用传。

### 4.4 同时查询历史指令

```http
GET /api/vla-debug/instructions?robot_type={robot_type}
```

响应示例：

```json
{
  "code": 200,
  "msg": "success",
  "data": {
    "items": [
      {
        "instruction_text": "pick up the cup",
        "robot_type": "W1",
        "last_used_at": "2026-07-02T08:35:00+00:00",
        "use_count": 3,
        "last_machine_id": "w1-01",
        "last_policy_service_id": "7fd5b8f0f5e14fd9a2eab8"
      }
    ]
  }
}
```

前端使用：

- 点击历史指令时，只把 `instruction_text` 填回输入框。
- 不需要恢复上次的机器或服务，避免误操作。
- Instruction History 会保存到 `nexus-api` 数据库里，服务重启后仍可查询。

## 5. 新增或更新 VLA Policy Service

接口：

```http
POST /api/vla-debug/robot-types/{robot_type}/policy-services
Content-Type: application/json
```

新增请求：

```json
{
  "name": "w1-pick-cup-0702",
  "endpoint": "10.18.103.121:3000"
}
```

更新请求：

```json
{
  "service_id": "7fd5b8f0f5e14fd9a2eab8",
  "name": "w1-pick-cup-0702",
  "endpoint": "10.18.103.121:3000"
}
```

响应为保存后的 service 对象。

前端校验建议：

- `name` 必填。
- `endpoint` 必填。
- endpoint 暂时按普通文本处理，不要强行要求 `http://`，因为当前 gRPC endpoint 形如 `host:port`。
- `protocol` 不出现在前端表单里，后端固定使用当前 VLA gRPC 协议。

注意：Policy Service 会保存到 `nexus-api` 数据库里，服务重启后仍可查询。

## 6. 启动 VLA 调试任务

接口：

```http
POST /api/vla-debug/tasks
Content-Type: application/json
```

请求：

```json
{
  "robot_type": "W1",
  "machine_id": "w1-01",
  "policy_service_id": "7fd5b8f0f5e14fd9a2eab8",
  "instruction": "pick up the cup",
  "execution_space": "joint",
  "execution_mode": "sync"
}
```

字段说明：

- `robot_type`：当前选中的机器人类型。
- `machine_id`：当前选中的在线机器。
- `policy_service_id`：当前选中的 VLA Policy Service。
- `instruction`：输入框里的自然语言指令。
- `execution_space`：`joint` 或 `eef`。默认建议 `joint`。
- `execution_mode`：`sync` 或 `async`。默认建议 `sync`。

说明：

- `sync`：ontology-core 执行完当前 action chunk 后，edge 再请求下一轮 VLA 推理。
- `async`：edge 更快地推送新 chunk，ontology-core 侧按异步替换/消费策略处理。

成功响应：

```json
{
  "code": 200,
  "msg": "success",
  "data": {
    "task_id": "b8bd59f8cc2a4b6aa3a2c",
    "machine_id": "w1-01",
    "robot_type": "W1",
    "policy_service_id": "7fd5b8f0f5e14fd9a2eab8",
    "instruction": "pick up the cup",
    "execution_space": "joint",
    "execution_mode": "sync",
    "invocation_id": "58cfd46a0b6a4e5f86d3",
    "status": "running",
    "started_at": "2026-07-02T08:36:00+00:00",
    "updated_at": "2026-07-02T08:36:00+00:00"
  }
}
```

前端收到成功后：

- 保存 `task_id`。
- 页面进入 running 状态。
- 禁用 Machine / Policy Service 切换。
- 禁用编辑当前运行使用的 Policy Service。
- Start 禁用。
- Stop 启用。
- Reset 仍然启用。
- 重新拉一次 machine state。

## 7. 停止任务

接口：

```http
POST /api/vla-debug/tasks/{task_id}/stop
```

响应示例：

```json
{
  "code": 200,
  "msg": "success",
  "data": {
    "task_id": "b8bd59f8cc2a4b6aa3a2c",
    "machine_id": "w1-01",
    "robot_type": "W1",
    "policy_service_id": "7fd5b8f0f5e14fd9a2eab8",
    "instruction": "pick up the cup",
    "execution_space": "joint",
    "execution_mode": "sync",
    "invocation_id": "58cfd46a0b6a4e5f86d3",
    "status": "stopped",
    "started_at": "2026-07-02T08:36:00+00:00",
    "updated_at": "2026-07-02T08:37:00+00:00"
  }
}
```

前端行为：

- 点击 Stop 后按钮 loading。
- 成功后清掉本地 `task_id`。
- 页面回到 idle 状态。
- 重新拉取 machine state。
- 如果失败，也要重新拉 machine state，避免页面状态和后端不一致。

## 8. Reset 机器

接口：

```http
POST /api/vla-debug/machines/{machine_id}/reset
```

Reset 语义：

- 如果该 Machine 有 active task，后端会先 stop，再 reset。
- 如果没有 active task，后端直接 reset。
- Reset 不清空当前选择，也不清空 instruction 输入框。

响应示例：

```json
{
  "code": 200,
  "msg": "success",
  "data": {
    "machine_id": "w1-01",
    "runtime_response": {
      "success": true
    }
  }
}
```

前端行为：

- Reset 可以一直显示，只要已选择 Machine。
- Reset 执行中，Start/Stop 暂时禁用。
- Reset 成功后清掉本地 `task_id`，并重新拉 machine state。

## 9. 查询机器状态

接口：

```http
GET /api/vla-debug/machines/{machine_id}/state
```

响应示例：

```json
{
  "code": 200,
  "msg": "success",
  "data": {
    "machine": {
      "machine_id": "w1-01",
      "name": "W1 Lab 01",
      "type_name": "W1",
      "is_online": true
    },
    "active_task": {
      "task_id": "b8bd59f8cc2a4b6aa3a2c",
      "machine_id": "w1-01",
      "robot_type": "W1",
      "policy_service_id": "7fd5b8f0f5e14fd9a2eab8",
      "instruction": "pick up the cup",
      "execution_space": "joint",
      "execution_mode": "sync",
      "invocation_id": "58cfd46a0b6a4e5f86d3",
      "status": "running",
      "started_at": "2026-07-02T08:36:00+00:00",
      "updated_at": "2026-07-02T08:36:00+00:00"
    }
  }
}
```

触发时机：

- 进入页面且已有默认/上次选中 Machine 时拉一次。
- 选择 Machine 后拉一次。
- Start / Stop / Reset 后拉一次。
- 用户手动刷新页面状态时拉一次。
- 不要定时轮询 state。运行中状态优先使用 Start/Stop/Reset 接口返回值维护；需要校准时再按事件触发拉取。
- 相机画面不要通过 state 轮询刷新，使用下面的 live-camera 状态和 SRS 播放地址。

## 10. 相机画面

页面显示的是 W1 的实时相机预览。预览不再走 VLA Debug Camera SSE，也不经过 `nexus-api` 转发图片；边端把四路 RGB 视角推到 SRS，前端从 `nexus-api` 获取播放地址后直接播放 SRS。

当前后端状态：

- Edge 上报 `live_camera.status`，其中包含 `rtmp_targets` 和发布状态。
- `nexus-api` 只负责查询状态、生成 SRS 播放 URL、下发 start/stop。
- SRS 按环境分别部署。测试环境入口是 `http://192.168.21.138:1985` / `http://192.168.21.138:18080`；生产环境入口由 `nexus-api` 配置返回，当前内网入口是 `http://192.168.21.139:1985` / `http://192.168.21.139:18084`。

查询预览状态：

```http
GET /api/vla-debug/machines/{machine_id}/live-camera
```

返回示例：

```json
{
  "machine_id": "w1",
  "state": "publishing",
  "rtmp_targets": [
    {
      "view": "head_binocular",
      "url": "rtmp://192.168.21.138/live/w1_head_binocular"
    }
  ],
  "playback": [
    {
      "view": "head_binocular",
      "stream": "w1_head_binocular",
      "rtmp_url": "rtmp://192.168.21.138/live/w1_head_binocular",
      "whep_url": "http://192.168.21.138:1985/rtc/v1/whep/?app=live&stream=w1_head_binocular",
      "flv_url": "http://192.168.21.138:18080/live/w1_head_binocular.flv"
    }
  ],
  "quality": {
    "width": 640,
    "height": 480,
    "fps": 15,
    "bitrate_bps": 1000000
  }
}
```

开启预览：

```http
POST /api/vla-debug/machines/{machine_id}/live-camera/start
```

关闭预览：

```http
POST /api/vla-debug/machines/{machine_id}/live-camera/stop
```

前端处理建议：

- 页面进入或切换 Machine 时调用 `GET live-camera`。
- 如果 `state=publishing`，按 `playback` 数组渲染四路播放器。
- 优先使用 `whep_url`；必要时用 `flv_url` 做 fallback。
- `whep_url` 不是普通 HTTP GET 接口，也不能直接设置为 `<video src>`。它需要 WebRTC/WHEP 客户端发 SDP offer。直接用 `fetch/axios` GET 这个地址会失败或 pending。
- 点击开启/关闭按钮时调用 start/stop，然后重新拉一次 `GET live-camera`。
- 状态只认 edge 上报的 `publishing` / `stopped`，前端不要持久化单独的预览状态。
- 旧的 `/camera/events` SSE 调试链路不再用于实时预览。

WHEP 最小播放代码：

```ts
import { SrsRtcWhipWhepAsync } from "./srs.sdk";

async function playWhep(video: HTMLVideoElement, whepUrl: string) {
  const sdk = new SrsRtcWhipWhepAsync();
  video.srcObject = sdk.stream;
  await sdk.play(whepUrl, { videoOnly: true });
  return () => sdk.close();
}
```

SRS 自带示例页面也可以验证单路流：

```text
http://192.168.21.138:18080/players/whep.html?schema=http&server=192.168.21.138&api=1985&app=live&stream={stream}&autostart=true
```

生产环境内网验证页面：

```text
http://192.168.21.139:18084/players/whep.html?schema=http&server=192.168.21.139&api=1985&app=live&stream={stream}&autostart=true
```

## 11. 按钮和页面状态规则

Start 可点击条件：

- 已选择 Machine。
- 已从 Machine 上拿到 `robot_type`。
- Machine `is_online === true` 且 `can_start_vla === true`。
- 已选择 VLA Policy Service。
- instruction trim 后非空。
- 当前没有 active task。
- 当前没有正在执行的 Start/Stop/Reset 请求。

Stop 可点击条件：

- 当前有 active task。
- 当前没有正在执行的 Stop/Reset 请求。

Reset 可点击条件：

- 已选择 Machine。
- 当前没有正在执行的 Reset 请求。

运行中必须禁用：

- Machine 切换。
- VLA Policy Service 切换。
- 编辑当前运行使用的 Policy Service。

运行中可以保留：

- instruction 输入框可以允许编辑，但不会影响当前任务；下一次 Start 才生效。
- Reset 按钮可以点击。

## 12. 错误处理

HTTP 响应有两类：

- 成功：HTTP 200，body 为 `{ "code": 200, "msg": "success", "data": ... }`。
- 失败：FastAPI 标准错误，常见 body 为 `{ "detail": "machine already has active VLA task" }`。

常见错误：

- `machine already has active VLA task`：该机器已有任务在跑。
- `VLA policy service not found`：选中的模型服务不存在，可能已被删除或前端缓存了过期 `service_id`。
- `policy service robot_type mismatch`：模型服务和当前机器人类型不一致。
- `machine robot_type mismatch`：机器和当前机器人类型不一致。
- Edge 下发失败：如果实际 Edge 长连接不可用，Start/Stop/Reset 会返回 409，前端展示 `detail` 即可。
- `unsupported execution_space`：`execution_space` 不是 `joint/eef`。
- `unsupported execution_mode`：`execution_mode` 不是 `sync/async`。

前端建议：

- 用 toast 或 message 展示 `detail`。
- Start 失败后回到 idle 状态，并重新拉 machine state。
- Stop/Reset 失败后重新拉 machine state。
- 如果接口返回 503 `vla debug service is disabled`，说明后端没有启用 VLA debug 服务，页面显示不可用状态。

## 13. 建议的前端状态模型

可以用这些局部状态：

```ts
type PagePhase = "idle" | "starting" | "running" | "stopping" | "resetting";

type ExecutionSpace = "joint" | "eef";
type ExecutionMode = "sync" | "async";

interface VLASelectionState {
  robotType?: string; // 从当前选中 Machine 的 robot_type 派生
  machineId?: string;
  policyServiceId?: string;
  instruction: string;
  executionSpace: ExecutionSpace;
  executionMode: ExecutionMode;
}

interface VLACameraState {
  eventSource?: EventSource;
  views: Array<{ name: string; encoding: string; data: string }>;
  status: "idle" | "connecting" | "streaming" | "error";
}
```

默认值：

- `executionSpace = "joint"`。
- `executionMode = "sync"`。

`active_task` 以服务端返回为准。如果本地认为 running，但 `GET state` 返回 `active_task = null`，前端应回到 idle。
不要用固定 interval 轮询 `GET state`；页面进入、切换 Machine、控制操作完成、用户点击刷新时再请求。

## 14. 第一版不要做的事情

这些不是当前版本范围：

- 不做模型服务启动/停止。
- 不做实验记录管理。
- 不保存 action chunk、执行轨迹、图像历史。
- 不做多用户锁。
- 不让浏览器直连 Edge。
- 不让浏览器直连 VLA gRPC 服务。
- 不做复杂 session 概念。

## 15. 接口清单

| 功能 | 方法 | 路径 |
| --- | --- | --- |
| 查询全部机器 | GET | `/api/vla-debug/machines` |
| 查询机器人类型 | GET | `/api/vla-debug/robot-types` |
| 查询类型下机器 | GET | `/api/vla-debug/robot-types/{robot_type}/machines` |
| 查询模型服务 | GET | `/api/vla-debug/robot-types/{robot_type}/policy-services` |
| 新增/更新模型服务 | POST | `/api/vla-debug/robot-types/{robot_type}/policy-services` |
| 查询历史指令 | GET | `/api/vla-debug/instructions?robot_type={robot_type}` |
| 启动任务 | POST | `/api/vla-debug/tasks` |
| 停止任务 | POST | `/api/vla-debug/tasks/{task_id}/stop` |
| Reset 机器 | POST | `/api/vla-debug/machines/{machine_id}/reset` |
| 查询机器状态 | GET | `/api/vla-debug/machines/{machine_id}/state` |
| 查询实时相机预览 | GET | `/api/vla-debug/machines/{machine_id}/live-camera` |
| 开启实时相机预览 | POST | `/api/vla-debug/machines/{machine_id}/live-camera/start` |
| 关闭实时相机预览 | POST | `/api/vla-debug/machines/{machine_id}/live-camera/stop` |

## 16. 最小联调路径

前端做完后，可以按这个顺序联调：

1. 打开页面，能看到 Machine 列表，每台机器带 `robot_type` 标签。
2. 选择一台 Machine 后查询实时相机预览，`state=publishing` 时按 `playback` 渲染多路播放器。
3. 根据选中 Machine 的 `robot_type` 查询/新增 VLA Policy Service。
4. 新增 VLA Policy Service，例如 `10.18.103.121:3000`。
5. 选择 policy service。
6. 输入 instruction。
7. 点击 Start，页面进入 running；相机预览播放器保持独立。
8. 点击 Stop，页面回到 idle；相机预览由实时相机开关单独控制。
9. 点击 Reset，页面保持当前选择，并刷新 machine state。
