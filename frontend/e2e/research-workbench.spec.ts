import { expect, test } from "@playwright/test";

const email = process.env.E2E_EMAIL;
const password = process.env.E2E_PASSWORD;
const tenant = process.env.E2E_TENANT_ID ?? "tenant_dev";
const fixtureTaskId = process.env.E2E_FIXTURE_TASK_ID;
const fixtureThreadId = process.env.E2E_FIXTURE_THREAD_ID;
const pendingTaskId = process.env.E2E_PENDING_TASK_ID;
const pendingThreadId = process.env.E2E_PENDING_THREAD_ID;

async function login(page: import("@playwright/test").Page) {
  await page.goto("/login");
  await page.getByLabel("租户 ID").fill(tenant);
  await page.getByLabel("邮箱").fill(email!);
  await page.getByLabel("密码").fill(password!);
  await page.getByRole("button", { name: "进入工作台" }).click();
  await expect(page.getByRole("heading", { name: /你好/ })).toBeVisible();
}

test.describe("real Phase 11 workbench", () => {
  test.skip(!email || !password, "E2E_EMAIL and E2E_PASSWORD are required for real infrastructure acceptance.");

  test("login, create two real tasks, restore cookie session, and switch task-scoped resources", async ({ page }) => {
    const stamp = Date.now();
    await login(page);

    await page.getByRole("link", { name: /研究工作台/ }).click();
    await page.getByPlaceholder("输入一个值得深入研究的问题…").fill(`[deep] Phase 11 browser acceptance ${stamp}: compare the strongest evidence for and against a testable primary explanation, then produce a cited report.`);
    await page.getByRole("button", { name: "开始研究 →" }).click();
    await expect(page).toHaveURL(/\/threads\/thread_[^/]+\/tasks\/task_[^/]+/);
    const firstUrl = page.url();
    const firstTaskId = firstUrl.split("/").at(-1)!;
    await expect(page.getByText(/正在|研究已完成/).first()).toBeVisible();
    await expect(page.getByText("事件已连接")).toBeVisible({ timeout: 20_000 });

    await page.reload();
    await expect(page).toHaveURL(firstUrl);
    await expect(page.getByText("研究对话")).toBeVisible();

    await page.getByPlaceholder("继续追问或提出新的研究方向…").fill(`Phase 11 follow-up ${stamp}: identify one implementation risk.`);
    await page.getByRole("button", { name: "开始研究 →" }).click();
    await expect(page).not.toHaveURL(firstUrl);
    await page.getByLabel("当前任务").selectOption(firstTaskId);
    await expect(page).toHaveURL(firstUrl);

    await page.getByRole("tab", { name: "搜索结果" }).click();
    await expect(page.getByText(/条证据|暂无证据/).first()).toBeVisible();
    await page.getByRole("tab", { name: "知识图谱" }).click();
    await expect(page.getByRole("button", { name: "查询图谱" })).toBeVisible();
    await page.getByPlaceholder("搜索实体与主张").fill("testable primary explanation");
    await page.getByRole("button", { name: "查询图谱" }).click();
    await expect(page.getByText(/个实体|查询任务知识图谱/).first()).toBeVisible();
    await page.getByRole("tab", { name: "可视化" }).click();
    await expect(page.getByText(/工作区尚为空|选择一个产物/).first()).toBeVisible();
    await page.getByRole("tab", { name: /研究报告/ }).click();
    await expect(page.getByText(/报告尚未生成|REPORT/).first()).toBeVisible();
  });

  test("read real Evidence, Neo4j provenance, MinIO visualization, ReportAst, and citation source", async ({ page }) => {
    test.skip(!fixtureTaskId || !fixtureThreadId, "A real Phase 11 resource fixture is required.");
    await login(page);
    await page.goto(`/threads/${fixtureThreadId!}/tasks/${fixtureTaskId!}`);
    await expect(page.getByText("研究已完成")).toBeVisible();

    await page.getByRole("tab", { name: "搜索结果" }).click();
    await expect(page.getByText("3 条证据")).toBeVisible();
    await page.getByRole("button", { name: /public design contract/ }).click();
    await expect(page.getByRole("link", { name: /打开 Web 来源/ })).toHaveAttribute("href", /^https:\/\//);
    await page.getByRole("button", { name: "关闭" }).click();

    await page.getByRole("tab", { name: "知识图谱" }).click();
    await page.getByPlaceholder("搜索实体与主张").fill("uses_authoritative_state");
    await page.getByRole("button", { name: "查询图谱" }).click();
    await expect(page.getByText("uses_authoritative_state")).toBeVisible();
    await page.getByRole("button", { name: "evidence_phase11_web" }).click();
    await expect(page.getByText("evidence_phase11_web", { exact: true })).toBeVisible();
    await page.getByRole("button", { name: "关闭" }).click();

    await page.getByRole("tab", { name: "可视化" }).click();
    await page.getByRole("button", { name: /visualizations\/evidence-balance\.svg/ }).click();
    await expect(page.getByRole("img", { name: "visualizations/evidence-balance.svg" })).toBeVisible();

    await page.getByRole("tab", { name: /研究报告/ }).click();
    await expect(page.getByRole("heading", { name: "Phase 11 可追溯研究工作台验收报告" })).toBeVisible();
    await page.getByRole("button", { name: "[cite_phase11_web]" }).click();
    await expect(page.getByRole("link", { name: /打开 Web 来源/ })).toHaveAttribute("href", "https://example.com/research-source");
    await page.screenshot({ path: "test-results/phase11-real-resource.png", fullPage: true });
  });

  test("restore a real PendingWrite after refresh and reject it through the public API", async ({ page }) => {
    test.skip(!pendingTaskId || !pendingThreadId, "A real PendingWrite fixture is required.");
    await login(page);
    await page.goto(`/threads/${pendingThreadId!}/tasks/${pendingTaskId!}`);
    await expect(page.getByRole("heading", { name: "数据库写入等待你的决定" })).toBeVisible();
    await expect(page.getByText("Phase 11 acceptance database")).toBeVisible();
    await expect(page.getByRole("button", { name: /继续/ })).toHaveCount(0);

    await page.reload();
    await expect(page.getByRole("heading", { name: "数据库写入等待你的决定" })).toBeVisible();
    await page.getByLabel("拒绝原因（可选）").fill("Phase 11 browser acceptance rejection");
    await page.getByRole("button", { name: "拒绝" }).click();
    await expect(page.getByRole("heading", { name: "数据库写入等待你的决定" })).toHaveCount(0);
  });
});
