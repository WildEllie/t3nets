const fs = require("fs");
const {
  Document, Packer, Paragraph, TextRun, Table, TableRow, TableCell,
  Header, Footer, AlignmentType, LevelFormat,
  HeadingLevel, BorderStyle, WidthType, ShadingType,
  PageNumber, PageBreak, TabStopType, TabStopPosition,
} = require("docx");

// --- Color palette ---
const BLUE = "1B4F72";
const LIGHT_BLUE = "D6EAF8";
const DARK_GRAY = "2C3E50";
const MEDIUM_GRAY = "5D6D7E";
const LIGHT_GRAY = "F2F3F4";
const WHITE = "FFFFFF";
const GREEN = "27AE60";
const ORANGE = "E67E22";
const RED = "E74C3C";

// --- Helpers ---
const border = { style: BorderStyle.SINGLE, size: 1, color: "BDC3C7" };
const borders = { top: border, bottom: border, left: border, right: border };
const noBorder = { style: BorderStyle.NONE, size: 0, color: WHITE };
const noBorders = { top: noBorder, bottom: noBorder, left: noBorder, right: noBorder };
const cellMargins = { top: 80, bottom: 80, left: 120, right: 120 };

function headerCell(text, width) {
  return new TableCell({
    borders,
    width: { size: width, type: WidthType.DXA },
    shading: { fill: BLUE, type: ShadingType.CLEAR },
    margins: cellMargins,
    children: [new Paragraph({ children: [new TextRun({ text, bold: true, color: WHITE, font: "Arial", size: 20 })] })],
  });
}

function cell(text, width, opts = {}) {
  const runs = typeof text === "string"
    ? [new TextRun({ text, font: "Arial", size: 20, color: DARK_GRAY, ...opts })]
    : text;
  return new TableCell({
    borders,
    width: { size: width, type: WidthType.DXA },
    shading: opts.shading ? { fill: opts.shading, type: ShadingType.CLEAR } : undefined,
    margins: cellMargins,
    children: [new Paragraph({ children: runs })],
  });
}

function para(text, opts = {}) {
  return new Paragraph({
    spacing: { after: opts.after ?? 160, before: opts.before ?? 0, line: opts.line ?? 276 },
    alignment: opts.alignment,
    children: typeof text === "string"
      ? [new TextRun({ text, font: "Arial", size: 22, color: DARK_GRAY, ...opts })]
      : text,
  });
}

function heading1(text) {
  return new Paragraph({
    heading: HeadingLevel.HEADING_1,
    spacing: { before: 360, after: 200 },
    children: [new TextRun({ text, font: "Arial", size: 36, bold: true, color: BLUE })],
  });
}

function heading2(text) {
  return new Paragraph({
    heading: HeadingLevel.HEADING_2,
    spacing: { before: 280, after: 160 },
    children: [new TextRun({ text, font: "Arial", size: 28, bold: true, color: DARK_GRAY })],
  });
}

function heading3(text) {
  return new Paragraph({
    heading: HeadingLevel.HEADING_3,
    spacing: { before: 200, after: 120 },
    children: [new TextRun({ text, font: "Arial", size: 24, bold: true, color: MEDIUM_GRAY })],
  });
}

function codePara(text) {
  return new Paragraph({
    spacing: { after: 80, before: 80 },
    indent: { left: 360 },
    children: [new TextRun({ text, font: "Courier New", size: 18, color: DARK_GRAY })],
  });
}

function bulletItem(text, opts = {}) {
  const runs = typeof text === "string"
    ? [new TextRun({ text, font: "Arial", size: 22, color: DARK_GRAY, ...opts })]
    : text;
  return new Paragraph({
    numbering: { reference: "bullets", level: 0 },
    spacing: { after: 80 },
    children: runs,
  });
}

function statusBadge(status) {
  const colors = { "Completed": GREEN, "In Progress": ORANGE, "Planned": MEDIUM_GRAY };
  return new TextRun({ text: ` ${status}`, font: "Arial", size: 18, bold: true, color: colors[status] || MEDIUM_GRAY });
}

// --- Build document ---
const doc = new Document({
  numbering: {
    config: [
      {
        reference: "bullets",
        levels: [{
          level: 0, format: LevelFormat.BULLET, text: "\u2022", alignment: AlignmentType.LEFT,
          style: { paragraph: { indent: { left: 720, hanging: 360 } } },
        }],
      },
      {
        reference: "numbers",
        levels: [{
          level: 0, format: LevelFormat.DECIMAL, text: "%1.", alignment: AlignmentType.LEFT,
          style: { paragraph: { indent: { left: 720, hanging: 360 } } },
        }],
      },
    ],
  },
  styles: {
    default: { document: { run: { font: "Arial", size: 22 } } },
    paragraphStyles: [
      { id: "Heading1", name: "Heading 1", basedOn: "Normal", next: "Normal", quickFormat: true,
        run: { size: 36, bold: true, font: "Arial", color: BLUE },
        paragraph: { spacing: { before: 360, after: 200 }, outlineLevel: 0 } },
      { id: "Heading2", name: "Heading 2", basedOn: "Normal", next: "Normal", quickFormat: true,
        run: { size: 28, bold: true, font: "Arial", color: DARK_GRAY },
        paragraph: { spacing: { before: 280, after: 160 }, outlineLevel: 1 } },
      { id: "Heading3", name: "Heading 3", basedOn: "Normal", next: "Normal", quickFormat: true,
        run: { size: 24, bold: true, font: "Arial", color: MEDIUM_GRAY },
        paragraph: { spacing: { before: 200, after: 120 }, outlineLevel: 2 } },
    ],
  },
  sections: [
    // ===================== COVER PAGE =====================
    {
      properties: {
        page: {
          size: { width: 12240, height: 15840 },
          margin: { top: 1440, right: 1440, bottom: 1440, left: 1440 },
        },
      },
      children: [
        new Paragraph({ spacing: { before: 3600 } }),
        new Paragraph({
          alignment: AlignmentType.CENTER,
          spacing: { after: 200 },
          children: [new TextRun({ text: "T3nets", font: "Arial", size: 72, bold: true, color: BLUE })],
        }),
        new Paragraph({
          alignment: AlignmentType.CENTER,
          spacing: { after: 600 },
          children: [new TextRun({ text: "System Architecture Document", font: "Arial", size: 36, color: MEDIUM_GRAY })],
        }),
        new Paragraph({
          alignment: AlignmentType.CENTER,
          border: { top: { style: BorderStyle.SINGLE, size: 6, color: BLUE, space: 20 } },
          spacing: { before: 200, after: 200 },
          children: [new TextRun({ text: "Multi-Tenant AI Agent Platform", font: "Arial", size: 28, color: DARK_GRAY })],
        }),
        new Paragraph({ spacing: { before: 1200 } }),
        new Paragraph({
          alignment: AlignmentType.CENTER,
          spacing: { after: 100 },
          children: [new TextRun({ text: "Version 1.0  |  February 25, 2026", font: "Arial", size: 22, color: MEDIUM_GRAY })],
        }),
        new Paragraph({
          alignment: AlignmentType.CENTER,
          children: [new TextRun({ text: "Current Phase: 2 (Multi-Tenancy)", font: "Arial", size: 22, color: GREEN, bold: true })],
        }),
      ],
    },

    // ===================== MAIN CONTENT =====================
    {
      properties: {
        page: {
          size: { width: 12240, height: 15840 },
          margin: { top: 1440, right: 1440, bottom: 1440, left: 1440 },
        },
      },
      headers: {
        default: new Header({
          children: [new Paragraph({
            alignment: AlignmentType.RIGHT,
            children: [
              new TextRun({ text: "T3nets Architecture", font: "Arial", size: 18, color: MEDIUM_GRAY, italics: true }),
            ],
          })],
        }),
      },
      footers: {
        default: new Footer({
          children: [new Paragraph({
            alignment: AlignmentType.CENTER,
            children: [
              new TextRun({ text: "Page ", font: "Arial", size: 18, color: MEDIUM_GRAY }),
              new TextRun({ children: [PageNumber.CURRENT], font: "Arial", size: 18, color: MEDIUM_GRAY }),
            ],
          })],
        }),
      },
      children: [
        // ============= 1. EXECUTIVE SUMMARY =============
        heading1("1. Executive Summary"),
        para("T3nets is a multi-tenant AI agent platform that connects teams to their productivity tools (Jira, GitHub, and more) through conversational AI powered by Anthropic Claude. The platform is designed for cost efficiency, security, and extensibility."),
        para([
          new TextRun({ text: "The core innovation is a ", font: "Arial", size: 22, color: DARK_GRAY }),
          new TextRun({ text: "hybrid routing engine", font: "Arial", size: 22, color: DARK_GRAY, bold: true }),
          new TextRun({ text: " that cuts AI costs by 50\u201360% compared to routing every message through Claude. Simple greetings are handled locally at zero cost, known skill triggers bypass the AI decision layer, and only ambiguous or complex queries engage the full Claude pipeline with tools.", font: "Arial", size: 22, color: DARK_GRAY }),
        ]),
        para("The system follows a cloud-agnostic architecture: all business logic lives in a portable core with zero cloud imports, while environment-specific adapters handle AWS (Bedrock, DynamoDB, Cognito), local development (SQLite, Anthropic API), and future clouds (Azure, GCP)."),

        // ============= 2. SYSTEM OVERVIEW =============
        heading1("2. System Overview"),

        heading2("2.1 High-Level Architecture"),
        para("The platform consists of four major layers, each independently deployable and testable:"),

        new Table({
          width: { size: 9360, type: WidthType.DXA },
          columnWidths: [2200, 3580, 3580],
          rows: [
            new TableRow({ children: [headerCell("Layer", 2200), headerCell("Components", 3580), headerCell("Purpose", 3580)] }),
            new TableRow({ children: [
              cell("Frontend", 2200, { bold: true }),
              cell("chat.html, login.html, onboard.html, settings.html", 3580),
              cell("Browser-based SPA-style UI with JWT auth", 3580),
            ]}),
            new TableRow({ children: [
              cell("Server", 2200, { bold: true, shading: LIGHT_GRAY }),
              cell("dev_server.py (local), server.py (AWS)", 3580, { shading: LIGHT_GRAY }),
              cell("HTTP API serving UI pages and data endpoints", 3580, { shading: LIGHT_GRAY }),
            ]}),
            new TableRow({ children: [
              cell("Core Engine", 2200, { bold: true }),
              cell("Router, Skills Registry, Memory, Models", 3580),
              cell("Cloud-agnostic business logic (agent/)", 3580),
            ]}),
            new TableRow({ children: [
              cell("Infrastructure", 2200, { bold: true, shading: LIGHT_GRAY }),
              cell("Terraform, ECS Fargate, API Gateway, DynamoDB", 3580, { shading: LIGHT_GRAY }),
              cell("AWS deployment with Cognito auth", 3580, { shading: LIGHT_GRAY }),
            ]}),
          ],
        }),

        heading2("2.2 Request Flow"),
        para("Every inbound message follows this path through the system:"),

        codePara("Browser \u2192 API Gateway (JWT validation) \u2192 ALB \u2192 ECS Fargate"),
        codePara("  \u2192 Server extracts auth (JWT \u2192 DynamoDB fallback)"),
        codePara("  \u2192 Router: Tier 1 (regex) \u2192 Tier 2 (rules) \u2192 Tier 3 (Claude)"),
        codePara("  \u2192 Skill execution via EventBus"),
        codePara("  \u2192 Save turn to ConversationStore (DynamoDB)"),
        codePara("  \u2192 JSON response to browser"),

        // ============= 3. CORE ENGINE =============
        heading1("3. Core Engine (agent/)"),
        para("All business logic lives in the agent/ directory with zero cloud imports. This makes the core portable across any deployment environment and trivially testable with mocks."),

        heading2("3.1 Hybrid Routing Engine"),
        para("The router is the heart of T3nets. It classifies every inbound message into one of three cost tiers:"),

        new Table({
          width: { size: 9360, type: WidthType.DXA },
          columnWidths: [1300, 2500, 1900, 1800, 1860],
          rows: [
            new TableRow({ children: [
              headerCell("Tier", 1300), headerCell("Handling", 2500), headerCell("Cost", 1900),
              headerCell("Latency", 1800), headerCell("Example", 1860),
            ]}),
            new TableRow({ children: [
              cell([new TextRun({ text: "Tier 1", font: "Arial", size: 20, bold: true, color: GREEN })], 1300),
              cell("Regex pattern match \u2192 canned response", 2500),
              cell("$0.00", 1900), cell("<1ms", 1800), cell("\"hi\", \"thanks\"", 1860),
            ]}),
            new TableRow({ children: [
              cell([new TextRun({ text: "Tier 2", font: "Arial", size: 20, bold: true, color: ORANGE })], 1300, { shading: LIGHT_GRAY }),
              cell("Skill trigger match \u2192 execute \u2192 Claude formats", 2500, { shading: LIGHT_GRAY }),
              cell("~$0.01", 1900, { shading: LIGHT_GRAY }), cell("1\u20132s", 1800, { shading: LIGHT_GRAY }),
              cell("\"sprint status\"", 1860, { shading: LIGHT_GRAY }),
            ]}),
            new TableRow({ children: [
              cell([new TextRun({ text: "Tier 3", font: "Arial", size: 20, bold: true, color: RED })], 1300),
              cell("Claude with tools \u2192 decides action \u2192 formats", 2500),
              cell("~$0.02\u20130.05", 1900), cell("2\u20134s", 1800), cell("Complex queries", 1860),
            ]}),
          ],
        }),

        para([
          new TextRun({ text: "At 100 messages/day, hybrid routing reduces monthly AI costs from ~$90\u2013150 to ~$30\u201360. ", font: "Arial", size: 22, color: DARK_GRAY }),
          new TextRun({ text: "Approximately 40% of messages are conversational (Tier 1), 30% rule-matched (Tier 2), and 30% AI-routed (Tier 3).", font: "Arial", size: 22, color: DARK_GRAY }),
        ], { before: 120 }),

        heading2("3.2 Skills System"),
        para("Skills are modular integrations that connect to external tools. Each skill is a directory containing:"),
        bulletItem([
          new TextRun({ text: "skill.yaml", font: "Courier New", size: 20, color: DARK_GRAY, bold: true }),
          new TextRun({ text: " \u2014 metadata, trigger phrases, parameter definitions", font: "Arial", size: 22, color: DARK_GRAY }),
        ]),
        bulletItem([
          new TextRun({ text: "worker.py", font: "Courier New", size: 20, color: DARK_GRAY, bold: true }),
          new TextRun({ text: " \u2014 async execute() function that calls external APIs", font: "Arial", size: 22, color: DARK_GRAY }),
        ]),

        para("Current skills: sprint_status (Jira), release_notes (Jira), ping (health check). The SkillRegistry loads skills at startup and provides tool definitions for Claude."),

        heading2("3.3 Interfaces"),
        para("Abstract base classes define the contracts between the core engine and adapters:"),

        new Table({
          width: { size: 9360, type: WidthType.DXA },
          columnWidths: [2800, 3280, 3280],
          rows: [
            new TableRow({ children: [headerCell("Interface", 2800), headerCell("Local Adapter", 3280), headerCell("AWS Adapter", 3280)] }),
            new TableRow({ children: [cell("AIProvider", 2800, { bold: true }), cell("Anthropic direct API", 3280), cell("Bedrock Converse API", 3280)] }),
            new TableRow({ children: [cell("ConversationStore", 2800, { bold: true, shading: LIGHT_GRAY }), cell("SQLite", 3280, { shading: LIGHT_GRAY }), cell("DynamoDB", 3280, { shading: LIGHT_GRAY })] }),
            new TableRow({ children: [cell("TenantStore", 2800, { bold: true }), cell("SQLite", 3280), cell("DynamoDB (single-table)", 3280)] }),
            new TableRow({ children: [cell("SecretsProvider", 2800, { bold: true, shading: LIGHT_GRAY }), cell(".env file", 3280, { shading: LIGHT_GRAY }), cell("Secrets Manager", 3280, { shading: LIGHT_GRAY })] }),
            new TableRow({ children: [cell("EventBus", 2800, { bold: true }), cell("DirectBus (sync)", 3280), cell("DirectBus (future: EventBridge)", 3280)] }),
          ],
        }),

        // ============= 4. AUTHENTICATION & MULTI-TENANCY =============
        new Paragraph({ children: [new PageBreak()] }),
        heading1("4. Authentication & Multi-Tenancy"),

        heading2("4.1 Authentication Flow"),
        para("Authentication uses AWS Cognito with PKCE OAuth 2.0. The flow works as follows:"),

        bulletItem("User visits /login \u2192 frontend redirects to Cognito Hosted UI"),
        bulletItem("User authenticates (email/password or social) \u2192 Cognito returns authorization code"),
        bulletItem("Frontend exchanges code for tokens at /callback \u2192 stores id_token in localStorage"),
        bulletItem("All API requests include Authorization: Bearer {id_token}"),
        bulletItem("API Gateway JWT authorizer validates token signature and expiry on the $default route"),
        bulletItem("Server-side middleware extracts sub, email, and custom:tenant_id from JWT payload"),

        heading2("4.2 User-Tenant Resolution (DynamoDB Fallback)"),
        para([
          new TextRun({ text: "This is the most recently implemented architecture change. ", font: "Arial", size: 22, color: DARK_GRAY }),
          new TextRun({ text: "The system uses a two-tier resolution strategy ", font: "Arial", size: 22, color: DARK_GRAY, bold: true }),
          new TextRun({ text: "to determine which tenant a user belongs to:", font: "Arial", size: 22, color: DARK_GRAY }),
        ]),

        new Table({
          width: { size: 9360, type: WidthType.DXA },
          columnWidths: [900, 2600, 2930, 2930],
          rows: [
            new TableRow({ children: [headerCell("Step", 900), headerCell("Source", 2600), headerCell("When Used", 2930), headerCell("Performance", 2930)] }),
            new TableRow({ children: [
              cell("1", 900, { bold: true }),
              cell("JWT custom:tenant_id", 2600, { bold: true }),
              cell("Token has tenant claim (most users)", 2930),
              cell("Fast path \u2014 no DB call", 2930),
            ]}),
            new TableRow({ children: [
              cell("2", 900, { bold: true, shading: LIGHT_GRAY }),
              cell("DynamoDB cognito-sub GSI", 2600, { bold: true, shading: LIGHT_GRAY }),
              cell("JWT lacks tenant_id (new users, stale tokens)", 2930, { shading: LIGHT_GRAY }),
              cell("GSI query by COGNITO#{sub}", 2930, { shading: LIGHT_GRAY }),
            ]}),
            new TableRow({ children: [
              cell("3", 900, { bold: true }),
              cell("Default tenant / onboarding", 2600, { bold: true }),
              cell("User not found in DB", 2930),
              cell("Redirect to onboarding wizard", 2930),
            ]}),
          ],
        }),

        para([
          new TextRun({ text: "Why this matters: ", font: "Arial", size: 22, color: DARK_GRAY, bold: true }),
          new TextRun({ text: "Cognito\u2019s custom:tenant_id is set after onboarding, but the JWT only reflects it after re-login. New users and users with stale tokens would lose their email identity without the DynamoDB fallback. The cognito-sub-lookup GSI enables cross-tenant user resolution by Cognito sub (always present in the JWT).", font: "Arial", size: 22, color: DARK_GRAY }),
        ], { before: 120 }),

        heading2("4.3 Tenant Isolation"),
        para("Data isolation is enforced at every layer:"),
        bulletItem([
          new TextRun({ text: "DynamoDB: ", font: "Arial", size: 22, color: DARK_GRAY, bold: true }),
          new TextRun({ text: "All keys are prefixed with TENANT#{tenant_id}. Users, settings, and channel mappings are partition-scoped.", font: "Arial", size: 22, color: DARK_GRAY }),
        ]),
        bulletItem([
          new TextRun({ text: "Secrets Manager: ", font: "Arial", size: 22, color: DARK_GRAY, bold: true }),
          new TextRun({ text: "Path-based isolation: /{project}/{env}/tenants/{tenant_id}/{integration}. IAM policy scopes access by path.", font: "Arial", size: 22, color: DARK_GRAY }),
        ]),
        bulletItem([
          new TextRun({ text: "Conversations: ", font: "Arial", size: 22, color: DARK_GRAY, bold: true }),
          new TextRun({ text: "Conversation keys include tenant_id as the partition key. No cross-tenant data leakage is possible.", font: "Arial", size: 22, color: DARK_GRAY }),
        ]),
        bulletItem([
          new TextRun({ text: "API Gateway: ", font: "Arial", size: 22, color: DARK_GRAY, bold: true }),
          new TextRun({ text: "JWT authorizer on the $default catch-all route. Public routes (login, onboard, auth config) bypass auth for the bootstrap flow.", font: "Arial", size: 22, color: DARK_GRAY }),
        ]),

        // ============= 5. DATA ARCHITECTURE =============
        heading1("5. Data Architecture"),

        heading2("5.1 DynamoDB Schema (Single-Table Design)"),
        para("T3nets uses two DynamoDB tables with PAY_PER_REQUEST billing:"),

        heading3("Tenants Table"),
        para("Single-table design storing tenants, users, and channel mappings. Uses item-type prefixes in sort keys:"),

        new Table({
          width: { size: 9360, type: WidthType.DXA },
          columnWidths: [2000, 2800, 2280, 2280],
          rows: [
            new TableRow({ children: [headerCell("Item Type", 2000), headerCell("PK", 2800), headerCell("SK", 2280), headerCell("GSI Keys", 2280)] }),
            new TableRow({ children: [
              cell("Tenant", 2000, { bold: true }),
              cell("TENANT#{id}", 2800), cell("META", 2280), cell("\u2014", 2280),
            ]}),
            new TableRow({ children: [
              cell("User", 2000, { bold: true, shading: LIGHT_GRAY }),
              cell("TENANT#{id}", 2800, { shading: LIGHT_GRAY }),
              cell("USER#{user_id}", 2280, { shading: LIGHT_GRAY }),
              cell("gsi2pk: COGNITO#{sub}", 2280, { shading: LIGHT_GRAY }),
            ]}),
            new TableRow({ children: [
              cell("Channel Map", 2000, { bold: true }),
              cell("TENANT#{id}", 2800),
              cell("CHANNEL#{type}#{id}", 2280),
              cell("gsi1pk: CHANNEL#{type}#{id}", 2280),
            ]}),
          ],
        }),

        heading3("Global Secondary Indexes"),

        new Table({
          width: { size: 9360, type: WidthType.DXA },
          columnWidths: [2800, 3280, 3280],
          rows: [
            new TableRow({ children: [headerCell("GSI Name", 2800), headerCell("Key Pattern", 3280), headerCell("Purpose", 3280)] }),
            new TableRow({ children: [
              cell("channel-mapping", 2800, { bold: true }),
              cell("gsi1pk = CHANNEL#{type}#{id}", 3280),
              cell("Resolve tenant from webhook payloads", 3280),
            ]}),
            new TableRow({ children: [
              cell("cognito-sub-lookup", 2800, { bold: true, shading: LIGHT_GRAY }),
              cell("gsi2pk = COGNITO#{sub}", 3280, { shading: LIGHT_GRAY }),
              cell("Resolve user/tenant when JWT lacks tenant_id", 3280, { shading: LIGHT_GRAY }),
            ]}),
          ],
        }),

        heading3("Conversations Table"),
        para("Stores chat history with 30-day TTL auto-expiry. Keys: PK = {tenant_id}, SK = {conversation_id}. Messages stored as a JSON array with optional metadata (route type, model, token count, user email)."),

        heading2("5.2 User Model"),
        para("The TenantUser model captures identity across systems:"),

        new Table({
          width: { size: 9360, type: WidthType.DXA },
          columnWidths: [2200, 2000, 5160],
          rows: [
            new TableRow({ children: [headerCell("Field", 2200), headerCell("Type", 2000), headerCell("Description", 5160)] }),
            new TableRow({ children: [cell("user_id", 2200), cell("string", 2000), cell("Unique within tenant", 5160)] }),
            new TableRow({ children: [cell("tenant_id", 2200, { shading: LIGHT_GRAY }), cell("string", 2000, { shading: LIGHT_GRAY }), cell("Owning tenant", 5160, { shading: LIGHT_GRAY })] }),
            new TableRow({ children: [cell("email", 2200), cell("string", 2000), cell("User email (from Cognito)", 5160)] }),
            new TableRow({ children: [cell("cognito_sub", 2200, { shading: LIGHT_GRAY }), cell("string", 2000, { shading: LIGHT_GRAY }), cell("Cognito subject ID \u2014 enables cross-tenant lookup via GSI", 5160, { shading: LIGHT_GRAY })] }),
            new TableRow({ children: [cell("role", 2200), cell("string", 2000), cell("admin or member", 5160)] }),
            new TableRow({ children: [cell("last_login", 2200, { shading: LIGHT_GRAY }), cell("string", 2000, { shading: LIGHT_GRAY }), cell("ISO 8601 timestamp", 5160, { shading: LIGHT_GRAY })] }),
            new TableRow({ children: [cell("channel_identities", 2200), cell("JSON", 2000), cell("{\"teams\": \"aad-id\", \"slack\": \"U12345\"}", 5160)] }),
          ],
        }),

        // ============= 6. AWS INFRASTRUCTURE =============
        new Paragraph({ children: [new PageBreak()] }),
        heading1("6. AWS Infrastructure"),

        heading2("6.1 Deployment Architecture"),
        para("The AWS deployment uses a fully Terraform-managed stack:"),

        codePara("Internet \u2192 API Gateway (HTTP v2, JWT auth) \u2192 VPC Link \u2192 ALB (internal)"),
        codePara("  \u2192 ECS Fargate (0.25 vCPU, 512MB) \u2192 DynamoDB + Secrets Manager + Bedrock"),

        heading2("6.2 Terraform Modules"),
        new Table({
          width: { size: 9360, type: WidthType.DXA },
          columnWidths: [2200, 4280, 2880],
          rows: [
            new TableRow({ children: [headerCell("Module", 2200), headerCell("Resources", 4280), headerCell("Monthly Cost", 2880)] }),
            new TableRow({ children: [cell("networking", 2200, { bold: true }), cell("VPC, 2 public + 2 private subnets, NAT Gateway, IGW", 4280), cell("~$32 (NAT)", 2880)] }),
            new TableRow({ children: [cell("data", 2200, { bold: true, shading: LIGHT_GRAY }), cell("DynamoDB tables (tenants + conversations), 2 GSIs", 4280, { shading: LIGHT_GRAY }), cell("~$1\u20132", 2880, { shading: LIGHT_GRAY })] }),
            new TableRow({ children: [cell("compute", 2200, { bold: true }), cell("ECS Fargate cluster, task definition, internal ALB, IAM roles", 4280), cell("~$5\u201310", 2880)] }),
            new TableRow({ children: [cell("api", 2200, { bold: true, shading: LIGHT_GRAY }), cell("API Gateway HTTP v2, JWT authorizer, public/private routes", 4280, { shading: LIGHT_GRAY }), cell("~$1\u20132", 2880, { shading: LIGHT_GRAY })] }),
            new TableRow({ children: [cell("secrets", 2200, { bold: true }), cell("Secrets Manager with path-based per-tenant isolation", 4280), cell("~$0.50", 2880)] }),
            new TableRow({ children: [cell("ecr", 2200, { bold: true, shading: LIGHT_GRAY }), cell("Container registry with lifecycle (keep last 10 images)", 4280, { shading: LIGHT_GRAY }), cell("~$0.50", 2880, { shading: LIGHT_GRAY })] }),
            new TableRow({ children: [cell("cognito", 2200, { bold: true }), cell("User pool, app client, custom:tenant_id attribute", 4280), cell("Free tier", 2880)] }),
          ],
        }),
        para([
          new TextRun({ text: "Total dev environment cost: ~$35\u201350/month ", font: "Arial", size: 22, color: DARK_GRAY, bold: true }),
          new TextRun({ text: "(excluding Bedrock usage, which is ~$10\u201350 depending on volume).", font: "Arial", size: 22, color: DARK_GRAY }),
        ], { before: 120 }),

        heading2("6.3 API Gateway Route Strategy"),
        para("The API Gateway uses a $default catch-all with JWT authorizer for all data endpoints. Specific public routes bypass auth for the bootstrap/onboarding flow:"),

        new Table({
          width: { size: 9360, type: WidthType.DXA },
          columnWidths: [3800, 1800, 3760],
          rows: [
            new TableRow({ children: [headerCell("Route", 3800), headerCell("Auth", 1800), headerCell("Purpose", 3760)] }),
            new TableRow({ children: [cell("$default (catch-all)", 3800, { bold: true }), cell("JWT Required", 1800), cell("All API data endpoints", 3760)] }),
            new TableRow({ children: [cell("GET /, /chat, /login, /settings, /onboard", 3800, { shading: LIGHT_GRAY }), cell("Public", 1800, { shading: LIGHT_GRAY }), cell("UI pages (must load for JS to check tokens)", 3760, { shading: LIGHT_GRAY })] }),
            new TableRow({ children: [cell("GET /api/auth/config, /api/auth/me", 3800), cell("Public", 1800), cell("Cognito config + current user info", 3760)] }),
            new TableRow({ children: [cell("POST /api/admin/tenants", 3800, { shading: LIGHT_GRAY }), cell("Public", 1800, { shading: LIGHT_GRAY }), cell("Tenant creation during onboarding", 3760, { shading: LIGHT_GRAY })] }),
            new TableRow({ children: [cell("POST /api/auth/assign-tenant", 3800), cell("Public", 1800), cell("Set Cognito tenant_id post-onboarding", 3760)] }),
          ],
        }),

        // ============= 7. ONBOARDING FLOW =============
        heading1("7. Onboarding Flow"),
        para("New users go through a step-by-step onboarding wizard that creates their tenant, configures integrations, and activates the account:"),

        bulletItem([
          new TextRun({ text: "Step 1 \u2014 Team Setup: ", font: "Arial", size: 22, color: DARK_GRAY, bold: true }),
          new TextRun({ text: "Enter team name, auto-generate tenant ID, create tenant in DynamoDB with status \u201Conboarding\u201D, create admin user with cognito_sub.", font: "Arial", size: 22, color: DARK_GRAY }),
        ]),
        bulletItem([
          new TextRun({ text: "Step 2 \u2014 Jira Integration: ", font: "Arial", size: 22, color: DARK_GRAY, bold: true }),
          new TextRun({ text: "Enter Jira URL, email, and API token. Test connection via /rest/api/3/myself. Save credentials to Secrets Manager under the new tenant\u2019s path.", font: "Arial", size: 22, color: DARK_GRAY }),
        ]),
        bulletItem([
          new TextRun({ text: "Step 3 \u2014 Activate: ", font: "Arial", size: 22, color: DARK_GRAY, bold: true }),
          new TextRun({ text: "PATCH tenant status from \u201Conboarding\u201D to \u201Cactive.\u201D Set custom:tenant_id in Cognito via admin API. User redirected to /login for a fresh token with the tenant claim.", font: "Arial", size: 22, color: DARK_GRAY }),
        ]),

        // ============= 8. REPOSITORY STRUCTURE =============
        heading1("8. Repository Structure"),

        codePara("t3nets/"),
        codePara("\u251C\u2500\u2500 agent/                 # Cloud-agnostic core (zero cloud imports)"),
        codePara("\u2502   \u251C\u2500\u2500 router/            # Hybrid routing engine (3 tiers)"),
        codePara("\u2502   \u251C\u2500\u2500 skills/            # Skill definitions (YAML + Python workers)"),
        codePara("\u2502   \u251C\u2500\u2500 interfaces/        # Abstract contracts (TenantStore, AIProvider, etc.)"),
        codePara("\u2502   \u251C\u2500\u2500 models/            # Shared dataclasses (Tenant, TenantUser, etc.)"),
        codePara("\u2502   \u251C\u2500\u2500 channels/          # Channel adapters (dashboard, future: Teams)"),
        codePara("\u2502   \u2514\u2500\u2500 memory/            # Conversation history management"),
        codePara("\u251C\u2500\u2500 adapters/"),
        codePara("\u2502   \u251C\u2500\u2500 local/             # SQLite, Anthropic API, .env, HTML pages"),
        codePara("\u2502   \u2514\u2500\u2500 aws/               # DynamoDB, Bedrock, Secrets Manager, Cognito"),
        codePara("\u251C\u2500\u2500 infra/aws/             # Terraform modules (7 modules)"),
        codePara("\u251C\u2500\u2500 scripts/               # deploy.sh, seed.sh, import-routes.sh"),
        codePara("\u251C\u2500\u2500 tests/                 # 26 tests (onboarding, auth, tenant, SQLite)"),
        codePara("\u251C\u2500\u2500 docs/                  # Architecture docs, ADRs, schema reference"),
        codePara("\u251C\u2500\u2500 Dockerfile             # ECS Fargate container image"),
        codePara("\u2514\u2500\u2500 version.txt            # Build number (auto-incremented on git commit)"),

        // ============= 9. DEVELOPMENT =============
        heading1("9. Development & Deployment"),

        heading2("9.1 Local Development"),
        codePara("pip install -e \".[local,dev]\""),
        codePara("python -m adapters.local.dev_server    # serves dashboard at http://localhost:8080"),
        para("Local mode uses SQLite for storage, the Anthropic API directly, and .env for secrets. No AWS credentials needed."),

        heading2("9.2 AWS Deployment"),
        codePara("cd infra/aws && terraform apply -var-file=environments/dev.tfvars"),
        codePara("./scripts/deploy.sh    # build container \u2192 push to ECR \u2192 update ECS"),
        codePara("./scripts/seed.sh      # populate DynamoDB + Secrets Manager"),
        para("Build numbers auto-increment on each git commit. Check the settings page to verify deployment."),

        heading2("9.3 Testing"),
        codePara("python -m unittest tests.test_onboarding -v    # 26 tests"),
        para("Tests cover tenant creation, activation, integration storage, admin API routing, Cognito sub lookup (in-memory and SQLite), and tenant model lifecycle."),

        // ============= 10. KEY DECISIONS =============
        heading1("10. Key Architecture Decisions"),

        new Table({
          width: { size: 9360, type: WidthType.DXA },
          columnWidths: [1300, 3400, 4660],
          rows: [
            new TableRow({ children: [headerCell("ADR", 1300), headerCell("Decision", 3400), headerCell("Rationale", 4660)] }),
            new TableRow({ children: [cell("001", 1300), cell("Cloud-agnostic core", 3400, { bold: true }), cell("Enables open-source, multi-cloud. Zero AWS imports in agent/", 4660)] }),
            new TableRow({ children: [cell("002", 1300, { shading: LIGHT_GRAY }), cell("Hybrid routing", 3400, { bold: true, shading: LIGHT_GRAY }), cell("50\u201360% AI cost reduction. Simple messages handled locally", 4660, { shading: LIGHT_GRAY })] }),
            new TableRow({ children: [cell("003", 1300), cell("DynamoDB single-table", 3400, { bold: true }), cell("Best practice for related entities. One table, multiple access patterns", 4660)] }),
            new TableRow({ children: [cell("004", 1300, { shading: LIGHT_GRAY }), cell("ECS Fargate over Lambda", 3400, { bold: true, shading: LIGHT_GRAY }), cell("Always warm, persistent connections, simpler local dev parity", 4660, { shading: LIGHT_GRAY })] }),
            new TableRow({ children: [cell("007", 1300), cell("Bedrock Converse API", 3400, { bold: true }), cell("Unified interface across model providers. Native tool_use", 4660)] }),
            new TableRow({ children: [cell("015", 1300, { shading: LIGHT_GRAY }), cell("Single-region Bedrock", 3400, { bold: true, shading: LIGHT_GRAY }), cell("Data residency compliance. No cross-region routing", 4660, { shading: LIGHT_GRAY })] }),
            new TableRow({ children: [cell("016", 1300), cell("Public routes for UI pages", 3400, { bold: true }), cell("Frontend must load before JS can check tokens", 4660)] }),
            new TableRow({ children: [cell("New", 1300, { shading: LIGHT_GRAY }), cell("DynamoDB auth fallback", 3400, { bold: true, shading: LIGHT_GRAY }), cell("Solves stale JWT problem. DynamoDB is source of truth for user\u2192tenant", 4660, { shading: LIGHT_GRAY })] }),
          ],
        }),

        // ============= 11. CURRENT STATUS & ROADMAP =============
        heading1("11. Current Status & Roadmap"),

        new Table({
          width: { size: 9360, type: WidthType.DXA },
          columnWidths: [1500, 4860, 3000],
          rows: [
            new TableRow({ children: [headerCell("Phase", 1500), headerCell("Scope", 4860), headerCell("Status", 3000)] }),
            new TableRow({ children: [cell("0", 1500, { bold: true }), cell("Design, prototype, local dev server, hybrid routing", 4860), cell([statusBadge("Completed")], 3000)] }),
            new TableRow({ children: [cell("1", 1500, { bold: true, shading: LIGHT_GRAY }), cell("AWS infrastructure (Terraform), Bedrock, DynamoDB adapters", 4860, { shading: LIGHT_GRAY }), cell([statusBadge("Completed")], 3000, { shading: LIGHT_GRAY })] }),
            new TableRow({ children: [cell("1b", 1500, { bold: true }), cell("Deploy to AWS, settings page, model selection, build numbers", 4860), cell([statusBadge("Completed")], 3000)] }),
            new TableRow({ children: [cell("2", 1500, { bold: true, shading: LIGHT_GRAY }), cell("Multi-tenancy: Cognito auth, onboarding wizard, DynamoDB user resolution", 4860, { shading: LIGHT_GRAY }), cell([statusBadge("In Progress")], 3000, { shading: LIGHT_GRAY })] }),
            new TableRow({ children: [cell("3", 1500, { bold: true }), cell("First external channel (Microsoft Teams via Azure Bot)", 4860), cell([statusBadge("Planned")], 3000)] }),
            new TableRow({ children: [cell("4", 1500, { bold: true, shading: LIGHT_GRAY }), cell("Expand skills (meeting prep, email triage, marketplace)", 4860, { shading: LIGHT_GRAY }), cell([statusBadge("Planned")], 3000, { shading: LIGHT_GRAY })] }),
            new TableRow({ children: [cell("5", 1500, { bold: true }), cell("Theming, SPA, CDN-served static assets", 4860), cell([statusBadge("Planned")], 3000)] }),
            new TableRow({ children: [cell("6", 1500, { bold: true, shading: LIGHT_GRAY }), cell("Long-term memory (S3), additional channels, OSS release", 4860, { shading: LIGHT_GRAY }), cell([statusBadge("Planned")], 3000, { shading: LIGHT_GRAY })] }),
          ],
        }),

        para([
          new TextRun({ text: "Phase 2 remaining work: ", font: "Arial", size: 22, color: DARK_GRAY, bold: true }),
          new TextRun({ text: "Deploy the cognito-sub-lookup GSI to DynamoDB (terraform apply), redeploy the ECS container with the new auth fallback code, seed a second tenant to verify data isolation, and test the full onboarding flow with a fresh user.", font: "Arial", size: 22, color: DARK_GRAY }),
        ], { before: 200 }),
      ],
    },
  ],
});

Packer.toBuffer(doc).then(buffer => {
  fs.writeFileSync("/sessions/amazing-stoic-keller/mnt/t3nets/t3nets-architecture.docx", buffer);
  console.log("Document created: t3nets-architecture.docx");
});
