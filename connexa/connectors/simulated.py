"""Simulated work connectors (Jira/HubSpot-ish). These behave like real providers —
they hold the full reauth / validate / execute contract — but no credentials exist,
so `authenticate_<x>` grants demo credentials and handlers run deterministically.
This is the sandbox lane: everything is exercisable end-to-end without vendor keys.
"""
from __future__ import annotations

import random
from datetime import datetime, timezone, timedelta

from pydantic import BaseModel

from .base import Connector, ToolSpec, ToolContext

_PEOPLE = ["Dana Whitfield", "Marcus Reed", "Priya Nair", "Tom Okafor", "Elena Rossi", "Jae Kim"]
_PROJECTS = ["CORE", "WEB", "OPS"]


def _fake_stamp(base: datetime, offset_hours: int) -> str:
    return (base - timedelta(hours=offset_hours)).isoformat().replace("+00:00", "Z")


class CurrentUserInput(BaseModel):
    pass


class CreateIssueInput(BaseModel):
    project: str
    summary: str
    description: str = ""
    priority: str = "medium"


class ListIssuesInput(BaseModel):
    max_results: int = 10


class GetIssueInput(BaseModel):
    key: str


class AssignIssueInput(BaseModel):
    key: str
    assignee: str


class JiraToolsConnector(Connector):
    """Issue-tracker connector (agent tools only; ticketing data plane uses helpdesk connectors)."""
    slug = "jira"
    name = "Issue Tracker"
    description = "Simulated issue tracker: create, list, read and assign issues for the connected user."
    auth_type = "token"
    real_lane_ready = False

    _issues_store: dict[str, dict] = {}   # in-memory demo fixture; reset on process start

    def build_tools(self) -> list[ToolSpec]:
        def get_current_user(ctx: ToolContext, _inp: None) -> dict:
            name = ctx.credentials.get("demo_name", "Demo User")
            return {"account_id": f"demo:{ctx.registered_user_id[:8]}", "display_name": name,
                    "active": True, "email": f"{name.split()[0].lower()}@demo.example"}

        def create_issue(ctx: ToolContext, inp: CreateIssueInput) -> dict:
            key = f"{inp.project.upper()}-{random.randint(100, 999)}"
            issue = {"key": key, "summary": inp.summary, "description": inp.description,
                     "priority": inp.priority, "status": "open",
                     "created": _fake_stamp(datetime.now(timezone.utc), 0),
                     "reporter": "Demo User"}
            self._issues_store[key] = issue
            return {"created": True, "issue": issue, "url": f"https://demo-tracker.example/browse/{key}"}

        def list_issues(ctx: ToolContext, inp: ListIssuesInput) -> dict:
            seeded = self._seeded_issues()
            rows = seeded + list(self._issues_store.values())
            return {"total": len(rows), "issues": rows[: max(1, inp.max_results)]}

        def get_issue(ctx: ToolContext, inp: GetIssueInput) -> dict:
            seeded = {i["key"]: i for i in self._seeded_issues()}
            issue = self._issues_store.get(inp.key) or seeded.get(inp.key.upper())
            if not issue:
                return {"error": f"Issue {inp.key} not found"}
            return issue

        def assign_issue(ctx: ToolContext, inp: AssignIssueInput) -> dict:
            issue = self._issues_store.get(inp.key)
            if not issue:
                return {"error": f"Issue {inp.key} not found"}
            issue["assignee"] = inp.assignee
            issue["status"] = "in_progress"
            return {"updated": True, "key": inp.key, "assignee": inp.assignee}

        return [
            ToolSpec(name="get_current_user", description="Get the connected user identity in the issue tracker.",
                     handler=get_current_user, input_model=CurrentUserInput, credit_type="low"),
            ToolSpec(name="create_issue", description="Create a new issue in a project.",
                     handler=create_issue, input_model=CreateIssueInput, credit_type="medium",
                     annotations={"readOnlyHint": False, "destructiveHint": False}),
            ToolSpec(name="list_issues", description="List recent issues (seeded demo backlog).",
                     handler=list_issues, input_model=ListIssuesInput, credit_type="low"),
            ToolSpec(name="get_issue", description="Fetch one issue by key (e.g. CORE-123).",
                     handler=get_issue, input_model=GetIssueInput, credit_type="low"),
            ToolSpec(name="assign_issue", description="Assign an existing issue to a person and move it to in_progress.",
                     handler=assign_issue, input_model=AssignIssueInput, credit_type="medium",
                     annotations={"readOnlyHint": False}),
        ]

    def _seeded_issues(self) -> list[dict]:
        base = datetime.now(timezone.utc)
        seeded = []
        for i, (project, summary, status, priority) in enumerate([
            ("CORE-101", "Onboarding flow crashes on step 3", "open", "high"),
            ("WEB-204", "Dark mode toggle resets on reload", "in_progress", "medium"),
            ("OPS-77", "Backup job timeout on Fridays", "open", "urgent"),
            ("CORE-102", "Add bulk update to queue module", "resolved", "low"),
            ("WEB-205", "Newsletter signup double-submits", "pending", "medium"),
        ]):
            seeded.append({"key": f"{project}", "summary": summary, "status": status,
                           "priority": priority, "assignee": _PEOPLE[i % len(_PEOPLE)],
                           "created": _fake_stamp(base, 24 * (i + 3)),
                           "reporter": _PEOPLE[(i + 2) % len(_PEOPLE)]})
        return seeded


class HubSpotLikeConnector(Connector):
    """CRM connector with BOTH agent tools and CRM data-plane sample generation."""
    slug = "hubspot"
    name = "Crm Cloud"
    description = "Simulated CRM: contacts, companies and deals (agent tools) plus CRM data-plane source."
    auth_type = "token"
    real_lane_ready = False

    class ContactSearchInput(BaseModel):
        email_or_name: str

    class CreateContactInput(BaseModel):
        first_name: str
        last_name: str = ""
        email: str

    class CreateDealInput(BaseModel):
        name: str
        amount: float = 0.0
        stage: str = "prospecting"

    def build_tools(self) -> list[ToolSpec]:
        def search_contact(ctx: ToolContext, inp: "HubSpotLikeConnector.ContactSearchInput") -> dict:
            needle = inp.email_or_name.lower()
            matches = [c for c in self._seed_contacts() if needle in (c.get("email") + " " + c.get("first_name", "") + " " + c.get("last_name", "")).lower()]
            return {"total": len(matches), "contacts": matches[:5]}

        def create_contact(ctx: ToolContext, inp: "HubSpotLikeConnector.CreateContactInput") -> dict:
            return {"created": True, "contact": {"id": str(random.randint(10_000, 99_999)),
                    "first_name": inp.first_name, "last_name": inp.last_name, "email": inp.email,
                    "stage": "lead"}}

        def create_deal(ctx: ToolContext, inp: "HubSpotLikeConnector.CreateDealInput") -> dict:
            if inp.stage not in ["prospecting", "proposal", "negotiation", "won", "lost"]:
                return {"error": f"invalid stage '{inp.stage}'"}
            return {"created": True, "deal": {"id": str(random.randint(10_000, 99_999)), **inp.model_dump()}}

        return [
            ToolSpec(name="search_contacts", description="Find CRM contacts by email or name.",
                     handler=search_contact, input_model=self.ContactSearchInput, credit_type="low"),
            ToolSpec(name="create_contact", description="Create a CRM contact.",
                     handler=create_contact, input_model=self.CreateContactInput, credit_type="medium",
                     annotations={"readOnlyHint": False}),
            ToolSpec(name="create_deal", description="Create a CRM deal with an amount and stage.",
                     handler=create_deal, input_model=self.CreateDealInput, credit_type="medium",
                     annotations={"readOnlyHint": False}),
        ]

    # --- CRM data-plane sample generation ---------------------------------------
    def generate_sample(self, category: str) -> list[dict]:
        if category != "crm":
            return []
        contacts = self._seed_contacts()
        deals = [
            {"remote_id": "HS-D1", "name": "Northwind expansion", "stage": "proposal", "amount": 48_000.0,
             "currency": "USD", "owner_name": _PEOPLE[0], "type": "deal",
             "contact_remote_id": contacts[0].get("remote_id")},
            {"remote_id": "HS-D2", "name": "Oz Labs renewal", "stage": "won", "amount": 12_500.0,
             "currency": "USD", "owner_name": _PEOPLE[3], "type": "deal"},
            {"remote_id": "HS-D3", "name": "Fieldstone pilot", "stage": "prospecting", "amount": 6_800.0,
             "currency": "USD", "owner_name": _PEOPLE[4], "type": "deal"},
            {"remote_id": "HS-D4", "name": "Brightline enterprise", "stage": "negotiation", "amount": 92_300.0,
             "currency": "USD", "owner_name": _PEOPLE[1], "type": "deal"},
            {"remote_id": "HS-D5", "name": "Muse & Co churn save", "stage": "lost", "amount": 3_100.0,
             "currency": "USD", "owner_name": _PEOPLE[5], "type": "deal"},
        ]
        return contacts + deals

    def _seed_contacts(self) -> list[dict]:
        base = datetime.now(timezone.utc)
        first_last = [("Ava", "Thompson"), ("Liam", "Garcia"), ("Maya", "Patel"), ("Noah", "Berg"),
                      ("Zoe", "Lindqvist"), ("Omar", "Haddad"), ("Ines", "Moreau"), ("Kai", "Tanaka")]
        companies = ["Northwind Ltd", "Oz Labs", "Fieldstone", "Brightline", "Muse & Co", "Delta Port", "Harborview", "Kestrel Wine"]
        stages = ["lead", "qualified", "customer", "qualified", "lead", "customer", "unqualified", "lead"]
        rows = []
        for i, (f, l) in enumerate(first_last):
            rows.append({
                "remote_id": f"CT-{5100 + i}", "first_name": f, "last_name": l,
                "email": f"{f.lower()}.{l.lower()}@{companies[i].split()[0].lower()}.example",
                "phone": f"+1-555-0{100 + i}", "company": companies[i], "stage": stages[i],
                "owner_name": _PEOPLE[i % len(_PEOPLE)],
                "created": _fake_stamp(base, 30 * (i + 1)),
                "type": "contact",
            })
        return rows


class HelpdeskLikeConnector(Connector):
    """Ticketing data-plane source with a couple of agent tools."""
    slug = "helpdesk"
    name = "Helpdesk Cloud"
    description = "Simulated helpdesk: tickets for the ticketing data plane, plus agent-side ticket tools."
    auth_type = "token"
    real_lane_ready = False

    class CreateTicketInput(BaseModel):
        subject: str
        body: str = ""
        priority: str = "medium"

    class GetTicketInput(BaseModel):
        ticket_id: str

    def build_tools(self) -> list[ToolSpec]:
        def create_ticket(ctx: ToolContext, inp: "HelpdeskLikeConnector.CreateTicketInput") -> dict:
            return {"created": True, "ticket": {"id": f"HD-{random.randint(1000, 9999)}", "subject": inp.subject,
                    "body": inp.body, "priority": inp.priority, "status": "open"}}

        def get_ticket(ctx: ToolContext, inp: "HelpdeskLikeConnector.GetTicketInput") -> dict:
            return {"ticket": {"id": inp.ticket_id, "subject": "Sample ticket subject", "status": "open",
                    "priority": "medium", "description": "Synthetic ticket loaded from the demo helpdesk."}}

        return [
            ToolSpec(name="create_ticket", description="Create a helpdesk ticket.",
                     handler=create_ticket, input_model=self.CreateTicketInput, credit_type="medium",
                     annotations={"readOnlyHint": False}),
            ToolSpec(name="get_ticket", description="Fetch one helpdesk ticket by id.",
                     handler=get_ticket, input_model=self.GetTicketInput, credit_type="low"),
        ]

    def generate_sample(self, category: str) -> list[dict]:
        if category != "ticketing":
            return []
        base = datetime.now(timezone.utc)
        subjects = [
            ("Login loop after password reset", "open", "high", ["auth", "bug"]),
            ("Export CSV missing last row", "in_progress", "medium", ["export"]),
            ("Request: SSO for tenant B", "pending", "low", ["sso", "request"]),
            ("Invoice 4471 shows wrong VAT", "open", "urgent", ["billing"]),
            ("Mobile push notifications delayed", "open", "medium", ["mobile", "perf"]),
            ("Add user roles to project view", "draft", "low", ["feature"]),
            ("Webhook endpoint rejects payloads > 1MB", "resolved", "high", ["webhooks"]),
            ("Slow dashboard for org with 10k rows", "in_progress", "medium", ["performance"]),
        ]
        rows = []
        for i, (title, status, priority, tags) in enumerate(subjects):
            rows.append({
                "remote_id": f"HD-{7200 + i}", "title": title, "status": status, "priority": priority,
                "assignee": _PEOPLE[i % len(_PEOPLE)], "tags": tags,
                "description": f"Synthetic detail for '{title}'.",
                "created": _fake_stamp(base, 12 * (i + 1)), "type": "ticket",
            })
        return rows


class PeopleOsConnector(Connector):
    """HRIS data-plane source: employees sync onto the Employee Common Model."""
    slug = "peopleos"
    name = "PeopleOS HRIS"
    description = "Simulated HRIS: employees, departments and managers for the HRIS data plane."
    auth_type = "token"
    real_lane_ready = False

    class SearchEmployeesInput(BaseModel):
        query: str

    def build_tools(self) -> list[ToolSpec]:
        def search_employees(ctx: ToolContext, inp: "PeopleOsConnector.SearchEmployeesInput") -> dict:
            needle = inp.query.lower()
            matches = [e for e in self.generate_sample("hris")
                       if needle in (e.get("first_name", "") + " " + e.get("last_name", "")).lower()]
            return {"total": len(matches), "employees": matches[:5]}

        return [ToolSpec(name="search_employees", description="Find employees by name in the HRIS.",
                         handler=search_employees, input_model=self.SearchEmployeesInput,
                         credit_type="low", annotations={"readOnlyHint": True})]

    def generate_sample(self, category: str) -> list[dict]:
        if category != "hris":
            return []
        base = datetime.now(timezone.utc)
        staff = [
            ("Dana", "Whitfield", "Engineering", "Staff Engineer", None, "full_time"),
            ("Marcus", "Reed", "Engineering", "Engineering Manager", "Dana Whitfield", "full_time"),
            ("Priya", "Nair", "Support", "Support Lead", None, "full_time"),
            ("Tom", "Okafor", "Support", "Support Specialist", "Priya Nair", "full_time"),
            ("Elena", "Rossi", "Finance", "Controller", None, "full_time"),
            ("Jae", "Kim", "Finance", "AP Clerk", "Elena Rossi", "part_time"),
            ("Sara", "Lindgren", "Design", "Product Designer", None, "contractor"),
            ("Noah", "Berg", "Sales", "Account Executive", None, "full_time"),
        ]
        rows = []
        for i, (f, l, dept, position, manager, emp_type) in enumerate(staff):
            rows.append({
                "remote_id": f"EMP-{400 + i}", "first_name": f, "last_name": l,
                "email": f"{f.lower()}.{l.lower()}@peopleos.example", "team": dept,   # 'team' = provider-specific field (mappable)
                "position": position, "manager": manager, "employment_type": emp_type,
                "type": "employee", "created": _fake_stamp(base, 720 * (i + 1)),
            })
        return rows


class LedgerlyConnector(Connector):
    """Accounting data-plane source: invoices sync onto the Invoice Common Model."""
    slug = "ledgerly"
    name = "Ledgerly Books"
    description = "Simulated accounting: invoices and vendors for the accounting data plane."
    auth_type = "token"
    real_lane_ready = False

    def build_tools(self) -> list[ToolSpec]:
        def noop(ctx: ToolContext, inp: None) -> dict:  # noqa: ARG001
            return {"ok": True}
        return [ToolSpec(name="ping", description="Availability check for Ledgerly Books.",
                         handler=noop, input_model=type("Ping", (BaseModel,), {}), credit_type="low",
                         annotations={"readOnlyHint": True, "idempotentHint": True})]

    def generate_sample(self, category: str) -> list[dict]:
        if category != "accounting":
            return []
        bills = [
            ("INV-2026-041", "paid", "Northwind Ltd", 4_800.0, "2026-09-30", "2026-10-30"),
            ("INV-2026-042", "submitted", "Oz Labs", 1_950.0, "2026-10-14", "2026-11-14"),
            ("INV-2026-043", "draft", "Fieldstone", 620.5, "2026-10-20", "2026-11-20"),
            ("INV-2026-044", "paid", "Brightline", 12_000.0, "2026-08-02", "2026-09-01"),
            ("INV-2026-045", "void", "Muse & Co", 310.0, "2026-07-11", "2026-08-10"),
            ("INV-2026-046", "submitted", "Delta Port", 7_300.0, "2026-10-25", "2026-11-25"),
        ]
        rows = []
        for i, (number, status, vendor, total, issued, due) in enumerate(bills):
            rows.append({
                "remote_id": f"LED-{900 + i}", "invoice_number": number, "status": status,
                "vendor": vendor, "total": total, "currency": "USD",
                "issued": issued, "due": due,
                "memo": f"Net-30 services for {vendor}.",
                "type": "invoice",
            })
        return rows


class SalespodLikeConnector(Connector):
    slug = "salespod"
    name = "SalesPod CRM"
    description = "Simulated sales CRM (CRM data-plane source, contact+deal emphasis)."
    auth_type = "token"
    real_lane_ready = False

    def build_tools(self) -> list[ToolSpec]:
        def noop(ctx: ToolContext, inp: None) -> dict:  # noqa: ARG001
            return {"ok": True}
        return [ToolSpec(name="ping", description="Availability check for SalesPod CRM.",
                         handler=noop, input_model=type("Ping", (BaseModel,), {}), credit_type="low",
                         annotations={"readOnlyHint": True, "idempotentHint": True})]

    def generate_sample(self, category: str) -> list[dict]:
        if category != "crm":
            return []
        base = datetime.now(timezone.utc)
        rows = []
        leads = [("Ingrid", "Solano", "Pinecrest Media", "customer", 1_500),
                 ("Devon", "Grant", "Apex Logistics", "lead", 900),
                 ("Chiara", "Vitale", "Vitale Studio", "qualified", 2_400)]
        for i, (f, l, company, stage, _a) in enumerate(leads):
            rows.append({"remote_id": f"SP-{900 + i}", "first_name": f, "last_name": l,
                         "email": f"{f.lower()}@{company.split()[0].lower()}.example",
                         "company": company, "stage": stage, "owner_name": _PEOPLE[i],
                         "type": "contact", "created": _fake_stamp(base, 40 * (i + 1))})
        rows.append({"remote_id": "SP-D1", "name": "Apex Logistics first fleet", "stage": "proposal",
                     "amount": 22_000.0, "currency": "USD", "owner_name": _PEOPLE[3], "type": "deal",
                     "created": _fake_stamp(base, 36)})
        return rows
