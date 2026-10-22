import { expect, test } from "@playwright/test";
import type { Page } from "@playwright/test";
const wid = "13193b83-1af2-427e-80f0-c3dbef4e9689";
const sid = "02b86ccc-201e-48d3-8d53-82faf4a3a191";
const aid = "c07af3f5-452a-4ceb-a339-c1447e536c23";
const approval = {
  approval_id: aid,
  workflow_id: wid,
  tool: "propose_mock_remediation",
  arguments: { action: "restart_demo_service" },
  risk: "simulated_write",
  description: "只登记模拟动作，不操作真实服务。",
};
const event = (type: string, data: object) =>
  `event: ${type}\ndata: ${JSON.stringify(data)}\n\n`;
const caps = {
  llm_mode: "mock",
  sessions: true,
  workflows: true,
  rag: true,
  mcp: true,
  tracing: true,
  tool_data_source: "mock_tools",
};
async function setup(page: Page, capabilities = caps) {
  await page.route("**/api/v1/capabilities", (route) =>
    route.fulfill({ json: capabilities }),
  );
  await page.route("**/api/v1/sessions", (route) =>
    route.fulfill({
      status: 201,
      json: { session_id: sid, expires_in_seconds: 1800 },
    }),
  );
}
async function token(page: Page) {
  await page
    .getByRole("button", { name: "连接设置", exact: true })
    .first()
    .click();
  await page.getByLabel("Workflow 审批令牌").fill("test-only-local-token");
  await page.getByRole("button", { name: "完成设置" }).click();
}
for (const decision of ["approve", "reject"]) {
  test(`session + workflow + ${decision} + trace`, async ({ page }) => {
    await setup(page);
    let status = "pending_approval";
    await page.route(`**/api/v1/workflows/${wid}`, (route) =>
      route.fulfill({
        json: {
          workflow_id: wid,
          status,
          resumable: status !== "completed",
          pending_approval: status === "pending_approval" ? approval : null,
          session_status: "committed",
        },
      }),
    );
    await page.route("**/api/v1/workflows?*", async (route) => {
      expect(route.request().postDataJSON()).toEqual({
        message: "mock remediation",
        session_id: sid,
      });
      expect(route.request().headers().authorization).toBe(
        "Bearer test-only-local-token",
      );
      await route.fulfill({
        contentType: "text/event-stream",
        headers: { "X-Trace-ID": "a".repeat(32) },
        body:
          event("workflow_start", { workflow_id: wid, status: "running" }) +
          event("node_progress", {
            node: "retrieve_runbook",
            phase: "completed",
            retrieval_status: "found",
            chunk_count: 2,
          }) +
          event("tool_result", {
            tool: "mcp_get_mock_redis_metrics",
            success: true,
            result: { hit_rate: 0.42 },
          }) +
          event("approval_required", { workflow_id: wid, status, approval }),
      });
    });
    await page.route(`**/api/v1/workflows/${wid}/approval?*`, async (route) => {
      expect(route.request().postDataJSON()).toEqual({
        approval_id: aid,
        decision,
      });
      status = "completed";
      await route.fulfill({
        contentType: "text/event-stream",
        headers: { "X-Trace-ID": "b".repeat(32) },
        body:
          event("workflow_resumed", { workflow_id: wid, status: "running" }) +
          event("final", {
            answer:
              decision === "approve"
                ? "模拟操作已登记，未操作真实服务。"
                : "已拒绝模拟操作。",
          }) +
          event("workflow_completed", {
            workflow_id: wid,
            status,
            session_status: "committed",
          }),
      });
    });
    await page.goto("/");
    await token(page);
    await page.getByRole("button", { name: "新建会话" }).click();
    await page
      .getByRole("textbox", { name: "诊断问题" })
      .fill("mock remediation");
    await page.getByRole("button", { name: "发送", exact: true }).click();
    await expect(
      page.getByRole("button", { name: "批准并继续" }),
    ).toBeEnabled();
    await expect(
      page.getByText("RAG · Runbook 检索 · 命中 2 个片段"),
    ).toBeVisible();
    await expect(
      page.getByText("MCP · mcp_get_mock_redis_metrics · 已返回"),
    ).toBeVisible();
    await page
      .getByRole("button", {
        name: decision === "approve" ? "批准并继续" : "拒绝并继续",
      })
      .click();
    await expect(
      page.getByText(
        decision === "approve"
          ? "模拟操作已登记，未操作真实服务。"
          : "已拒绝模拟操作。",
        { exact: true },
      ),
    ).toBeVisible();
    await expect(page.getByRole("button", { name: "批准并继续" })).toHaveCount(
      0,
    );
    await expect(page.getByRole("link", { name: /Trace 02/ })).toHaveAttribute(
      "href",
      /opspilot-tempo/,
    );
    const storage = await page.evaluate(
      () => JSON.stringify(sessionStorage) + JSON.stringify(localStorage),
    );
    expect(storage).not.toContain("test-only-local-token");
    expect(storage).not.toContain("mock remediation");
    if (decision === "approve")
      await page.screenshot({
        path: "/tmp/opspilot-console-workflow.png",
        fullPage: true,
      });
  });
}
test("basic diagnosis fallback, server errors and responsive empty state", async ({
  page,
}) => {
  await setup(page, {
    ...caps,
    workflows: false,
    sessions: false,
    rag: false,
    mcp: false,
    tracing: false,
  });
  await page.route("**/api/v1/diagnose/stream", (route) =>
    route.fulfill({
      contentType: "text/event-stream",
      body:
        event("agent_start", { max_steps: 6 }) +
        event("error", { code: "llm_timeout", message: "timeout" }),
    }),
  );
  await page.goto("/");
  await expect(page.getByLabel("诊断模式")).toHaveValue("diagnose");
  await page.screenshot({
    path: "/tmp/opspilot-console-desktop.png",
    fullPage: true,
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(
    page.getByRole("heading", { name: "今天，我们排查什么？" }),
  ).toBeVisible();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBeTruthy();
  await page.screenshot({
    path: "/tmp/opspilot-console-mobile.png",
    fullPage: true,
  });
  await page.getByRole("textbox", { name: "诊断问题" }).fill("检查 CPU");
  await page.getByRole("button", { name: "发送", exact: true }).click();
  await expect(
    page.getByRole("alert").filter({ hasText: "模型请求超时" }),
  ).toBeVisible();
});
test("follow-up carries the same server session ID", async ({ page }) => {
  await setup(page, { ...caps, workflows: false });
  const requests: unknown[] = [];
  await page.route("**/api/v1/diagnose/stream", (route) => {
    requests.push(route.request().postDataJSON());
    return route.fulfill({
      contentType: "text/event-stream",
      body: event("final", { answer: `回答 ${requests.length}` }),
    });
  });
  await page.goto("/");
  await page.getByRole("button", { name: "开启多轮会话" }).click();
  for (const [index, question] of ["检查 Redis", "继续分析"].entries()) {
    await page.getByRole("textbox", { name: "诊断问题" }).fill(question);
    await page.getByRole("button", { name: "发送", exact: true }).click();
    await expect(
      page.getByText(`回答 ${index + 1}`, { exact: true }),
    ).toBeVisible();
    await expect(page.getByRole("textbox", { name: "诊断问题" })).toBeEnabled();
  }
  expect(requests).toEqual([
    { message: "检查 Redis", session_id: sid },
    { message: "继续分析", session_id: sid },
  ]);
});
test("explicitly restores a failed workflow and never auto-resumes", async ({
  page,
}) => {
  await setup(page);
  let resumes = 0;
  await page.route(`**/api/v1/workflows/${wid}`, (route) =>
    route.fulfill({
      json: {
        workflow_id: wid,
        status: resumes ? "completed" : "failed",
        resumable: !resumes,
        error_code: resumes ? null : "connection_interrupted",
        pending_approval: null,
      },
    }),
  );
  await page.route(`**/api/v1/workflows/${wid}/resume?*`, (route) => {
    resumes++;
    return route.fulfill({
      contentType: "text/event-stream",
      body:
        event("workflow_resumed", { workflow_id: wid, status: "running" }) +
        event("final", { answer: "恢复后完成诊断" }) +
        event("workflow_completed", { workflow_id: wid, status: "completed" }),
    });
  });
  await page.goto("/");
  await page
    .getByRole("button", { name: "连接设置", exact: true })
    .first()
    .click();
  await page.getByLabel("Workflow 审批令牌").fill("test-only-local-token");
  await page.getByLabel("查找已有工作流").fill(wid);
  await page.getByRole("button", { name: "查询并打开" }).click();
  await expect(page.getByRole("button", { name: "恢复执行" })).toBeEnabled();
  expect(resumes).toBe(0);
  await page.getByRole("button", { name: "恢复执行" }).click();
  await expect(page.getByText("恢复后完成诊断", { exact: true })).toBeVisible();
  expect(resumes).toBe(1);
});

test("reload retains workflow ID but clears token; status restores approval", async ({
  page,
}) => {
  await setup(page);
  await page.route(`**/api/v1/workflows/${wid}`, (route) =>
    route.fulfill({
      json: {
        workflow_id: wid,
        status: "pending_approval",
        resumable: true,
        pending_approval: approval,
      },
    }),
  );
  await page.goto("/");
  await page
    .getByRole("button", { name: "连接设置", exact: true })
    .first()
    .click();
  await page.getByLabel("Workflow 审批令牌").fill("test-only-local-token");
  await page.getByLabel("查找已有工作流").fill(wid);
  await page.getByRole("button", { name: "查询并打开" }).click();
  await expect(page.getByRole("button", { name: "批准并继续" })).toBeEnabled();
  await page.reload();
  await page.getByRole("button", { name: "刷新状态" }).click();
  await expect(page.getByRole("dialog")).toBeVisible();
  await expect(page.getByLabel("Workflow 审批令牌")).toHaveValue("");
  await page.getByLabel("Workflow 审批令牌").fill("test-only-local-token");
  await page.getByRole("button", { name: "完成设置" }).click();
  await page.getByRole("button", { name: "刷新状态" }).click();
  await expect(page.getByRole("button", { name: "批准并继续" })).toBeEnabled();
  await expect(page.getByRole("button", { name: "恢复执行" })).toBeDisabled();
});

test("authentication errors are visible inside the settings dialog", async ({
  page,
}) => {
  await setup(page);
  await page.route(`**/api/v1/workflows/${wid}`, (route) =>
    route.fulfill({
      status: 403,
      json: { error: { code: "workflow_unauthorized" } },
    }),
  );
  await page.goto("/");
  await page
    .getByRole("button", { name: "连接设置", exact: true })
    .first()
    .click();
  await page.getByLabel("Workflow 审批令牌").fill("incorrect-token");
  await page.getByLabel("查找已有工作流").fill(wid);
  await page.getByRole("button", { name: "查询并打开" }).click();
  await expect(page.getByRole("dialog").getByRole("alert")).toContainText(
    "令牌不正确",
  );
});
