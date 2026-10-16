import { fileURLToPath } from "node:url";
import { expect, test, type Browser, type Page } from "@playwright/test";
import { signIn, trackErrors, uid } from "./helpers";
import {
  FAKES,
  join,
  openChannel,
  openDm,
  openSlack,
  post,
} from "./slack-helpers";

// One story in order: the seed leaves integrations disabled and unconnected, the admin connects,
// a member links, then the agent uses them.
test.describe.configure({ mode: "serial" });

const AGENT = { timeout: 90_000 }; // an agent reply; generous for loaded machines and CI runners

const card = (page: Page, title: string) =>
  page.getByRole("region", { name: title, exact: true });

async function integrations(page: Page, email: string) {
  await signIn(page, email);
  await page
    .getByRole("navigation", { name: "Main" })
    .getByRole("link", { name: "Integrations" })
    .click();
  await expect(
    page.getByRole("heading", { level: 1, name: "Integrations" }),
  ).toBeVisible();
}

/** Approve on a stand-in consent page as the given account. */
async function consent(page: Page, account: RegExp | string) {
  await page.getByRole("radio", { name: account }).check();
  await page.getByRole("button", { name: "Allow" }).click();
}

async function say(page: Page, who: string, channel: string, text: string) {
  await openSlack(page, "T0ACME", who);
  await openChannel(page, channel);
  return post(page, text);
}

async function threadOf(page: Page, ts: string) {
  const message = page.locator(`[data-testid="message"][data-ts="${ts}"]`);
  await expect(message.getByRole("button", { name: /\d+ repl/ })).toBeVisible(
    AGENT,
  );
  await message.getByRole("button", { name: /\d+ repl/ }).click();
  const thread = page.getByTestId("thread");
  await expect(thread).toBeVisible();
  await expect(thread).toContainText("✅ Done", AGENT);
  return thread;
}

test("an admin enables and connects Jira and GitHub through the providers", async ({
  page,
}) => {
  const errors = trackErrors(page);
  await integrations(page, "alice@acme.test");
  const jira = card(page, "Jira");
  await expect(jira.getByTestId("jira-connection")).toContainText(
    "not connected",
  );
  await jira.getByLabel("Enable Jira").check();
  await expect(
    jira.getByRole("button", { name: "Link my account" }),
  ).toBeVisible();

  await jira.getByRole("button", { name: "Connect Jira" }).click();
  await expect(page).toHaveTitle("Jira wants to access your Atlassian account");
  await consent(page, /Alice Admin/);
  await expect(page.getByText("Jira is connected.")).toBeVisible();
  await expect(jira.getByTestId("jira-connection")).toContainText(
    "connected as cloud-acme",
  );

  const github = card(page, "GitHub");
  await github.getByLabel("Enable GitHub").check();
  await github.getByRole("button", { name: "Connect GitHub" }).click();
  await consent(page, "alice-gh installing on acme");
  await expect(page.getByText("GitHub is connected.")).toBeVisible();
  await expect(github.getByTestId("github-connection")).toContainText(
    "connected as acme",
  );

  await github.getByRole("button", { name: "Check now" }).click();
  await expect(github.getByText(/\d+ repositories visible\./)).toBeVisible();
  await expect(github.getByText("Healthy")).toBeVisible();
  expect(errors).toEqual([]);
});

test("a cancelled consent changes nothing", async ({ page }) => {
  await integrations(page, "alice@acme.test");
  await card(page, "Jira").getByRole("button", { name: "Reconnect" }).click();
  await page.getByRole("button", { name: "Deny" }).click();
  await expect(
    page.getByText("The connection was cancelled at the provider."),
  ).toBeVisible();
  await expect(card(page, "Jira").getByTestId("jira-connection")).toContainText(
    "connected as cloud-acme", // the org connection is to the Jira site
  );
});

test("a member links their own Jira and GitHub accounts", async ({ page }) => {
  await integrations(page, "bob@acme.test");
  const jira = card(page, "Jira");
  await expect(jira.getByLabel("Enable Jira")).toBeDisabled();
  await expect(
    jira.getByRole("button", { name: /Connect|Reconnect|Disconnect/ }),
  ).toHaveCount(0);
  await jira.getByRole("button", { name: "Link my account" }).click();
  await consent(page, /Bob Builder/);
  await expect(page.getByText("Your Jira account is linked.")).toBeVisible();
  await expect(jira.getByTestId("jira-link")).toContainText(
    "linked as acc-bob",
  );

  const github = card(page, "GitHub");
  await github.getByRole("button", { name: "Link my account" }).click();
  await consent(page, "bob-gh (bob@acme.test)");
  await expect(page.getByText("Your GitHub account is linked.")).toBeVisible();
  await expect(github.getByTestId("github-link")).toContainText(
    "linked as bob-gh",
  );
});

test("a QA user schedules a daily Jira report and gets exact numbers", async ({
  browser,
  page,
}) => {
  // The Jira stand-in's issues were created relative to its reset at the start of the run: in the
  // last day PAY-104 and PAY-106 were created, PAY-105 and PAY-106 resolved; PAY-101, PAY-102 and
  // PAY-104 are open (two To Do, unassigned; one In Progress).
  const t = uid();
  const bob = await (await browser.newContext()).newPage();
  await signIn(bob, "bob@acme.test");
  await bob
    .getByRole("navigation", { name: "Main" })
    .getByRole("link", { name: "Schedules" })
    .click();
  await bob.getByRole("button", { name: "New schedule" }).click();
  const dialog = bob.getByRole("dialog");
  await dialog.getByLabel("Name").fill(`PAY daily ${t}`);
  await dialog.getByLabel(/Jira progress/).check();
  await dialog.getByLabel("Jira project key").fill("PAY");
  await dialog.getByLabel("Cadence").selectOption("daily");
  // A run time just ahead: "Run now" then reports on the day since yesterday at that time.
  const at = new Date(Date.now() + 120_000);
  const time = `${String(at.getUTCHours()).padStart(2, "0")}:${String(at.getUTCMinutes()).padStart(2, "0")}`;
  await dialog.getByLabel("Time", { exact: true }).fill(time);
  await dialog.getByLabel("Time zone").selectOption("UTC");
  await dialog.getByRole("button", { name: "Create schedule" }).click();
  const row = bob.getByRole("listitem", { name: `PAY daily ${t}` });
  await expect(row).toContainText("Jira progress of PAY");
  await row.getByRole("button", { name: "Run now" }).click();
  await expect(
    row.getByText("Running now: the report follows shortly."),
  ).toBeVisible({
    timeout: 30_000,
  });

  await openSlack(page, "T0ACME", "Bob Builder");
  await openDm(page);
  const report = page
    .getByTestId("message")
    .filter({ hasText: `PAY daily ${t}` })
    .first();
  await expect(report).toContainText(
    "Created: 3 · Resolved: 2 · Open now: 3",
    AGENT,
  );
  await expect(report).toContainText("Open by status: To Do 2, In Progress 1");
  await expect(report).toContainText("Open by priority: High 1, Medium 2");
  await expect(report).toContainText("Open and unassigned: 2");
  await expect(report).toContainText("PAY-10");
  await expect(report).toContainText("Sources");
});

test("the agent answers from Jira and refuses a project the requester cannot browse", async ({
  page,
}) => {
  const ts = await say(
    page,
    "Bob Builder",
    "bugs",
    "@agent what is the status of PAY-101?",
  );
  const thread = await threadOf(page, ts);
  await expect(thread).toContainText("PAY-101");
  await expect(thread).toContainText("Checkout returns 500");

  const sec = await say(
    page,
    "Bob Builder",
    "bugs",
    "@agent what is happening with SEC-3?",
  );
  const refused = await threadOf(page, sec);
  await expect(refused).toContainText("can't browse the project of SEC-3");
  await expect(refused).not.toContainText("staging token");
});

test("a private repository answer goes only to the requester", async ({
  page,
}) => {
  const ts = await say(
    page,
    "Bob Builder",
    "bugs",
    "@agent who owns src/pricing/cart.py in acme/cart-service?",
  );
  const thread = await threadOf(page, ts);
  // Not everyone in #bugs has linked a GitHub account that can read acme/cart-service.
  await expect(thread).toContainText("I sent you the answer privately");
  await expect(thread).not.toContainText("@bob-gh");
  await openDm(page);
  await expect(
    page.getByTestId("message").filter({ hasText: "@bob-gh" }).last(),
  ).toBeVisible();
});

test("a credential mode override makes each person use their own account", async ({
  browser,
  page,
}) => {
  const admin = await (await browser.newContext()).newPage();
  await integrations(admin, "alice@acme.test");
  const jira = card(admin, "Jira");
  await jira.getByText(/^Operations \(/).click();
  await jira
    .getByLabel("Credential for get_issue")
    .selectOption("user_required");
  await expect(jira.getByText(/1 overridden/)).toBeVisible();

  // Alice has no Jira link of her own; Bob has.
  const alice = await say(
    page,
    "Alice Admin",
    "bugs",
    "@agent what is the status of PAY-102?",
  );
  await expect(await threadOf(page, alice)).toContainText(
    "Link your Jira account",
  );
  const bob = await say(
    page,
    "Bob Builder",
    "bugs",
    "@agent what is the status of PAY-102?",
  );
  await expect(await threadOf(page, bob)).toContainText("Refunds for EU cards");

  await jira.getByRole("button", { name: "Reset" }).click();
  await expect(jira.getByText(/overridden/)).toHaveCount(0);
  await expect(jira.getByLabel("Credential for get_issue")).toHaveValue(
    "org_with_check",
  );
});

test("a custom MCP server needs approval before it can be enabled", async ({
  page,
}) => {
  const name = `Status ${uid()}`;
  const key = `mcp_${name.toLowerCase().replace(" ", "_")}`;
  await integrations(page, "alice@acme.test");
  await page.getByRole("button", { name: "Add MCP server" }).click();
  const dialog = page.getByRole("dialog");
  await dialog.getByLabel("Name").fill(name);
  await dialog.getByLabel("URL").fill("http://fakes:4100/mcp/status");
  await dialog.getByLabel("API key").fill("mcp-key-local");
  await dialog.getByRole("button", { name: "Add server" }).click();

  const server = card(page, name);
  await expect(server.getByText("Waiting for approval")).toBeVisible();
  await expect(server.getByLabel(`Enable ${name}`)).toHaveCount(0);
  await expect(server.getByTestId(`${key}-connection`)).toContainText(
    "API key stored",
  );

  await server.getByRole("button", { name: "Approve" }).click();
  await expect(server.getByText("2 tools available.")).toBeVisible();
  await expect(server.getByText("Healthy")).toBeVisible();
  await expect(server.getByText("Waiting for approval")).toHaveCount(0);
  await server.getByLabel(`Enable ${name}`).check();
  await server.getByText(/^Operations \(2/).click();
  // Tool annotations decide the tier: a destructive tool is irreversible (always approved).
  await expect(server.getByTestId(`op-${key}-restart_service`)).toContainText(
    "Irreversible",
  );
  await expect(server.getByTestId(`op-${key}-status_incidents`)).toContainText(
    "Read",
  );

  // A wrong key: approved, but the check reports it broken.
  const broken = `Broken ${uid()}`;
  await page.getByRole("button", { name: "Add MCP server" }).click();
  await dialog.getByLabel("Name").fill(broken);
  await dialog.getByLabel("URL").fill("http://fakes:4100/mcp/status");
  await dialog.getByLabel("API key").fill("not-the-key");
  await dialog.getByRole("button", { name: "Add server" }).click();
  await card(page, broken).getByRole("button", { name: "Approve" }).click();
  await expect(
    card(page, broken).getByText("Broken", { exact: true }),
  ).toBeVisible();
});

// ------------------------------------------------------------------ Example 1: tickets from chat

type Issue = {
  key: string;
  summary: string;
  reporter: string;
  assignee: string | null;
};

async function issues(contains: string): Promise<Issue[]> {
  const r = await fetch(
    `${FAKES}/_admin/atlassian/issues?contains=${encodeURIComponent(contains)}`,
  );
  return (await r.json()) as Issue[];
}

/** A new channel with the given people (Bob, Quinn, Alice by default) and the agent, allowed by
 * Alice in the console (its members synced), so tests do not read each other's messages. */
async function ticketsChannel(
  browser: Browser,
  people: string[] = ["U0BOB", "U0QUINN", "U0ALICE"],
  prefix = "tickets",
): Promise<string> {
  const name = `${prefix}-${uid()}`;
  const created = await fetch(`${FAKES}/_admin/slack/channels`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ team: "T0ACME", name, members: [] }),
  });
  const { id } = (await created.json()) as { id: string };
  for (const user of ["U0ACMEBOT", ...people]) await join("T0ACME", id, user);
  const admin = await (await browser.newContext()).newPage();
  await signIn(admin, "alice@acme.test");
  await admin
    .getByRole("navigation", { name: "Main" })
    .getByRole("link", { name: "Slack" })
    .click();
  const allow = admin.getByLabel(`Allow the agent in ${name}`);
  await expect(allow).toBeVisible({ timeout: 15_000 });
  await allow.check();
  await expect(allow).toBeEnabled();
  await expect(async () => {
    await admin.reload();
    await expect(
      admin.getByTestId(`channel-${name}`).locator("td").nth(2),
    ).toHaveText(String(people.length), {
      timeout: 1_000,
    });
  }).toPass({ timeout: 45_000 });
  await admin.close();
  return name;
}

/** Open the thread of a message as soon as it has a reply (the task may still be waiting). */
async function repliesTo(page: Page, ts: string) {
  const message = page.locator(`[data-testid="message"][data-ts="${ts}"]`);
  await expect(message.getByRole("button", { name: /\d+ repl/ })).toBeVisible(
    AGENT,
  );
  await message.getByRole("button", { name: /\d+ repl/ }).click();
  const thread = page.getByTestId("thread");
  await expect(thread).toBeVisible();
  return thread;
}

test("an admin proposes the service catalog from GitHub and Jira and confirms it", async ({
  page,
}) => {
  await integrations(page, "alice@acme.test");
  // Proposals read CODEOWNERS within the admin's own access: Alice links her GitHub account.
  const github = card(page, "GitHub");
  await github.getByRole("button", { name: "Link my account" }).click();
  await consent(page, "alice-gh (alice@acme.test)");
  await expect(github.getByTestId("github-link")).toContainText(
    "linked as alice-gh",
  );

  await page
    .getByRole("navigation", { name: "Main" })
    .getByRole("link", { name: "Catalog" })
    .click();
  await expect(
    page.getByRole("heading", { level: 1, name: "Service catalog" }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Propose services" }).click();
  await expect(page.getByTestId("proposal-status")).toContainText(
    "Last proposal: 5 proposed",
    AGENT,
  );
  const proposed = page.getByRole("list", { name: "Proposed services" });
  const cart = proposed.getByRole("listitem", { name: "cart-service" });
  await expect(cart).toContainText(
    "Repository acme/cart-service · Jira PAY / Checkout",
  );
  await expect(cart).toContainText(
    "CODEOWNERS /src/pricing/ in acme/cart-service",
  );
  await expect(cart).toContainText("lead of Jira component PAY/Checkout");
  await cart.getByText("Why this was proposed").click();
  await expect(cart).toContainText(
    "@acme/platform (no member with this linked GitHub account)",
  );
  const refunds = proposed.getByRole("listitem", { name: "Refunds" });
  await expect(refunds).toContainText("lead of Jira component PAY/Refunds");

  await cart.getByRole("button", { name: "Confirm" }).click();
  await refunds.getByRole("button", { name: "Confirm" }).click();
  const confirmed = page.getByRole("list", { name: "Confirmed services" });
  await expect(
    confirmed.getByRole("listitem", { name: "cart-service" }),
  ).toBeVisible();
  await expect(
    confirmed.getByRole("listitem", { name: "Refunds" }),
  ).toBeVisible();
  page.once("dialog", (d) => void d.accept());
  await proposed
    .getByRole("listitem", { name: "Webhooks" })
    .getByRole("button", { name: "Archive" })
    .click();
  await expect(
    proposed.getByRole("listitem", { name: "Webhooks" }),
  ).toHaveCount(0);
});

test("Example 1 in Ask mode: tickets are created after approval, in the requester's name", async ({
  browser,
  page,
}) => {
  const channel = await ticketsChannel(browser);
  await say(
    page,
    "Quinn Tester",
    channel,
    "Checkout fails with a 500 for carts over 20 items again",
  );
  await say(page, "Quinn Tester", channel, "Discount codes fail at checkout");
  await say(page, "Alice Admin", channel, "Refund emails are sent twice");
  const ts = await say(
    page,
    "Bob Builder",
    channel,
    `@agent create Jira tickets for the problems reported today in #${channel}`,
  );
  const thread = await repliesTo(page, ts);
  const approvals = thread
    .getByTestId("message")
    .filter({ hasText: "Approval needed" });
  await expect(approvals).toHaveCount(2, AGENT); // requested together
  await expect(thread).toContainText("Waiting for 2 approvals");
  const discount = approvals.filter({
    hasText: "Discount codes fail at checkout",
  });
  await expect(discount).toContainText(
    "Assignee: Bob Builder (bob@acme.test), owner of cart-service",
  );
  const refunds = approvals.filter({ hasText: "Refund emails are sent twice" });
  await expect(refunds).toContainText(
    "Assignee: Alice Admin (alice@acme.test), owner of Refunds",
  );
  await discount.getByRole("button", { name: "Approve" }).click();
  await refunds.getByRole("button", { name: "Approve" }).click();
  await expect(thread).toContainText("✅ Done", AGENT);
  await expect(thread).toContainText("looks like PAY-101"); // a duplicate is not filed again
  const [ticket] = await issues("Discount codes fail at checkout");
  expect(ticket).toMatchObject({ reporter: "acc-bob", assignee: "acc-bob" });
  await expect(thread.getByRole("link", { name: ticket.key })).toBeVisible();
  const [refund] = await issues("Refund emails are sent twice");
  expect(refund).toMatchObject({ reporter: "acc-bob", assignee: "acc-alice" });
});

test("Example 1 in Auto mode: tickets are created right away and linked in the thread", async ({
  browser,
  page,
}) => {
  // Writes use the requester's own Jira account: Alice links hers.
  const admin = await (await browser.newContext()).newPage();
  await integrations(admin, "alice@acme.test");
  await card(admin, "Jira")
    .getByRole("button", { name: "Link my account" })
    .click();
  await consent(admin, /Alice Admin/);
  await expect(card(admin, "Jira").getByTestId("jira-link")).toContainText(
    "linked as acc-alice",
  );
  await admin.close();

  const channel = await ticketsChannel(browser);
  await say(page, "Quinn Tester", channel, "Gift cards fail at checkout");
  await say(
    page,
    "Bob Builder",
    channel,
    "Refund totals are wrong for partial refunds",
  );
  const ts = await say(
    page,
    "Alice Admin",
    channel,
    `@agent create Jira tickets for the problems reported today in #${channel}`,
  );
  const thread = await threadOf(page, ts);
  await expect(
    thread.getByTestId("message").filter({ hasText: "Approval needed" }),
  ).toHaveCount(0);
  const [gift] = await issues("Gift cards fail at checkout");
  expect(gift).toMatchObject({ reporter: "acc-alice", assignee: "acc-bob" });
  const [partial] = await issues("Refund totals are wrong");
  expect(partial).toMatchObject({
    reporter: "acc-alice",
    assignee: "acc-alice",
  });
  await expect(thread.getByRole("link", { name: gift.key })).toBeVisible();
  await expect(thread.getByRole("link", { name: partial.key })).toBeVisible();
});

// ------------------------------------------------------------------ Example 2: investigate and plan

test("an admin connects Cloud Logging and points a service at its logs", async ({
  page,
}) => {
  await integrations(page, "alice@acme.test");
  const logs = card(page, "Google Cloud Logging");
  await logs
    .getByLabel("Service account key file")
    .setInputFiles(
      fileURLToPath(
        new URL("../../dev/gcp/service-account-dev.json", import.meta.url),
      ),
    );
  await logs.getByRole("button", { name: "Connect" }).click();
  await expect(
    logs.getByText(
      /Connected as agent-reader@acme-prod\.iam\.gserviceaccount\.com/,
    ),
  ).toBeVisible();
  await expect(
    logs.getByTestId("google_cloud_logging-connection"),
  ).toContainText("connected as agent-reader");
  await logs.getByRole("button", { name: "Check now" }).click();
  await expect(
    logs.getByText("Logs of project acme-prod are readable."),
  ).toBeVisible();

  await page
    .getByRole("navigation", { name: "Main" })
    .getByRole("link", { name: "Catalog" })
    .click();
  const cart = page
    .getByRole("list", { name: "Confirmed services" })
    .getByRole("listitem", { name: "cart-service" });
  await cart.getByRole("button", { name: "Edit" }).click();
  const dialog = page.getByRole("dialog");
  await dialog.getByLabel("Log project").fill("acme-prod");
  await dialog.getByLabel("Resource type").fill("cloud_run_revision");
  await dialog.getByRole("button", { name: "Save" }).click();
  await expect(cart).toContainText("Logs acme-prod");
});

test("Example 2: a ticket is investigated into a sourced plan, refined in the thread", async ({
  browser,
  page,
}) => {
  // Bob and Alice may both read the repository, the project and the logs: the plan stays here.
  const channel = await ticketsChannel(
    browser,
    ["U0BOB", "U0ALICE"],
    "incident",
  );
  const ts = await say(
    page,
    "Bob Builder",
    channel,
    "@agent investigate PAY-101 and plan a fix",
  );
  const thread = await threadOf(page, ts);
  await expect(thread).toContainText(
    "Plan: Checkout returns 500 for carts with 20 or more items",
  );
  await expect(thread).toContainText("confidence high");
  await expect(thread).toContainText(
    "IndexError: list assignment index out of range",
  );
  await expect(thread).toContainText(
    "Preallocate price slots for faster totals",
  );
  await expect(thread).toContainText("[reproduce] Write a failing test");
  await expect(thread).toContainText("Sources");
  await expect(
    thread.getByRole("link", { name: /src\/pricing\/cart\.py/ }).first(),
  ).toBeVisible();

  await page
    .locator("#thread-composer")
    .fill("@agent it only happens for carts with discounts");
  await page.locator("#thread-send").click();
  await expect(
    thread.getByText(/Revised: Rechecked with the follow-up/),
  ).toBeVisible(AGENT);
  await expect(thread).toContainText("On your follow-up");
});

// ------------------------------------------------------------------ Example 2: implement and raise a PR

test("Example 2: the plan becomes a pull request; a review comes back and is addressed", async ({
  browser,
  page,
}) => {
  const channel = await ticketsChannel(browser, ["U0BOB", "U0ALICE"], "change");
  const ts = await say(
    page,
    "Bob Builder",
    channel,
    "@agent investigate PAY-101 and plan a fix",
  );
  const thread = await threadOf(page, ts);
  await expect(thread).toContainText(
    "Plan: Checkout returns 500 for carts with 20 or more items",
  );

  // "Implement the plan" in the thread: tests first in a sandbox, reviewed, checked, then the push
  // waits for Bob's approval (Ask mode).
  await page
    .locator("#thread-composer")
    .fill("@agent looks right, go ahead and implement the plan");
  await page.locator("#thread-send").click();
  const approval = thread
    .getByTestId("message")
    .filter({ hasText: "Approval needed" });
  await expect(approval).toHaveCount(1, { timeout: 180_000 });
  await expect(approval).toContainText(
    "open a pull request in acme/cart-service",
  );
  await expect(approval).toContainText("Verified: All tests pass.");
  await expect(approval).toContainText("tests/test_cart_many_items.py");
  await approval.getByRole("button", { name: "Approve" }).click();
  const opened = thread.getByRole("link", { name: /^acme\/cart-service#\d+$/ });
  await expect(opened).toBeVisible(AGENT);
  await expect(thread).toContainText(
    "The new test fails without the fix and passes with it.",
  );
  await expect(thread).toContainText("Review: approved.");
  const prUrl = (await opened.getAttribute("href")) ?? "";
  const number = Number(prUrl.split("/").pop());

  // The pull request has the change and says what was verified.
  const pr = await (await browser.newContext()).newPage();
  await pr.goto(prUrl);
  await expect(pr.getByRole("heading", { level: 1 })).toContainText("PAY-101");
  await expect(pr.locator("h3.file")).toHaveText([
    "src/pricing/cart.py",
    "tests/test_cart_many_items.py",
  ]);
  await expect(pr.locator("pre.diff").first()).toContainText(
    "return sum(item.price for item in items)",
  );
  await expect(pr.locator("pre.body")).toContainText(
    "The new test fails without the fix and passes with it.",
  );
  await expect(pr.getByText(/test: (success|in_progress)/)).toBeVisible();

  // A reviewer asks for a change on GitHub: the review comes back to the thread.
  const reviewed = await fetch(`${FAKES}/_admin/github/review`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({
      repository: "acme/cart-service",
      number,
      login: "alice-gh",
      state: "changes_requested",
      body: "Nearly there",
      comments: [
        {
          path: "src/pricing/cart.py",
          line: 1,
          body: "Please add a docstring to total",
        },
      ],
    }),
  });
  expect(reviewed.ok).toBe(true);
  await expect(thread).toContainText(
    `alice-gh requested changes on acme/cart-service#${number}`,
    AGENT,
  );
  await expect(thread).toContainText("Please add a docstring to total");

  // "Address the review comments" revises the same pull request.
  await page
    .locator("#thread-composer")
    .fill("@agent please address the review comments");
  await page.locator("#thread-send").click();
  const second = thread
    .getByTestId("message")
    .filter({ hasText: "Approval needed" });
  await expect(second).toHaveCount(2, { timeout: 180_000 });
  await expect(second.last()).toContainText(
    `Push a revision to acme/cart-service#${number}`,
  );
  await second.last().getByRole("button", { name: "Approve" }).click();
  await expect(thread).toContainText(
    `Pushed a revision to acme/cart-service#${number}`,
    AGENT,
  );
  await pr.reload();
  await expect(pr.locator("pre.diff").first()).toContainText(
    "Return the sum of the items' prices.",
  );
  await expect(
    pr.locator(".comment").filter({ hasText: /^.*Pushed [0-9a-f]{7}/ }),
  ).toHaveCount(1);
  await pr.close();
});
