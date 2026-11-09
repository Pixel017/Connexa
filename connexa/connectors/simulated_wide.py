"""Simulated data-lane connectors powering the two newest Common Model categories:
ATS (Candidate) and File Storage (File + Folder).

These are DEMO lanes like the original simulated providers: payloads are
synthesized locally (honestly labeled everywhere), and they exist so the full
surface — link flow, diff-aware sync, field mappings, scopes, webhooks,
write-through APIs — is exercisable per category end to end. Real-lane builds
of both categories are on the features registry roadmap.
"""
from __future__ import annotations

from .base import Connector


class HirePodConnector(Connector):
    """ATS demo lane — an applicant-tracking provider with provider-style field
    names (job_title / office / stage / recruiter / skills) so field mappings
    have real work to do."""
    slug = "hirepod"
    name = "HirePod ATS"
    description = ("ATS lane (demo data): sync job candidates into the Candidate Common "
                   "Model. Provider fields like job_title/stage map onto the Common Model "
                   "through per-account field mappings.")
    auth_type = "token"

    _NAMES = [("Maya", "Reyes", "maya.reyes@example.dev", "Northwind Ltd"),
              ("Omar", "Haddad", "omar.haddad@example.dev", "Brightline"),
              ("Lena", "Kim", "lena.kim@example.dev", "Callisto Health"),
              ("Tom", "Wagner", "tom.wagner@example.dev", "Vela Robotics"),
              ("Pia", "Nordin", "pia.nordin@example.dev", "Northwind Ltd"),
              ("Ravi", "Shah", "ravi.shah@example.dev", "Forma Labs"),
              ("Jules", "Bernard", "jules.bernard@example.dev", "Kettleworks"),
              ("Ada", "Ionescu", "ada.ionescu@example.dev", "Brightline")]
    _STAGES = ["screening", "interview", "applied", "offer", "interview",
               "rejected", "applied", "hired"]

    def build_tools(self):
        return []

    def generate_sample(self, category: str) -> list[dict]:
        if category != "ats":
            return []
        roles = ["Senior Backend Engineer", "Product Designer", "Data Platform Engineer",
                 "Engineering Manager", "Support Engineer", "ML Engineer"]
        offices = ["Remote EU", "Austin", "Berlin", "Remote"]
        rows = []
        for index, (first, last, email, company) in enumerate(self._NAMES):
            rows.append({
                "remote_id": f"CAN-{1042 + index}",
                "type": "candidate",
                "first_name": first,
                "last_name": last,
                "email": email,
                "current_company": company,
                # provider-style names (mappable onto role/location/application_status/tags)
                "job_title": roles[index % len(roles)],
                "office": offices[index % len(offices)],
                "stage": self._STAGES[index],
                "recruiter": ["Dana Cole", "Priya Nair", "Sam Ortiz"][index % 3],
                "skills": [["python", "postgres"], ["figma", "research"],
                           ["spark", "airflow"], ["kubernetes"], ["support", "sql"],
                           ["pytorch"], ["ruby"], ["typescript"]][index],
                "score": [82, 74, 91, 66, 78, 59, 88, 95][index],
                "source": ["referral", "careers-site", "linkedin", "referral",
                           "careers-site", "event", "linkedin", "referral"][index],
            })
        return rows


class VaultfileConnector(Connector):
    """File Storage demo lane — a document-store provider with folders and files
    using provider names (content_type / size / path / owner)."""
    slug = "vaultfile"
    name = "VaultFile"
    description = ("File Storage lane (demo data): sync folders and files into the "
                   "File / Folder Common Models, with provider names mapped onto the "
                   "normalized schema.")
    auth_type = "token"

    def build_tools(self):
        return []

    def generate_sample(self, category: str) -> list[dict]:
        if category != "filestorage":
            return []
        folders = [
            {"remote_id": "fold-eng", "type": "folder", "name": "Engineering",
             "path": "", "parent": ""},
            {"remote_id": "fold-eng-specs", "type": "folder", "name": "Specs",
             "path": "/Engineering", "parent": "Engineering"},
            {"remote_id": "fold-people", "type": "folder", "name": "People Ops",
             "path": "", "parent": ""},
        ]
        files = [
            {"remote_id": "file-arch", "type": "file", "name": "architecture.md",
             "content_type": "text/markdown", "size": 15234, "path": "/Engineering",
             "owner": "dana@acme.example", "tags": ["docs", "system"]},
            {"remote_id": "file-sync", "type": "file", "name": "sync-engine-spec.md",
             "content_type": "text/markdown", "size": 9712, "path": "/Engineering/Specs",
             "owner": "priya@acme.example", "tags": ["docs"]},
            {"remote_id": "file-onb", "type": "file", "name": "onboarding-v2.pdf",
             "content_type": "application/pdf", "size": 281440, "path": "/People Ops",
             "owner": "hr@acme.example", "tags": ["hr"]},
            {"remote_id": "file-brand", "type": "file", "name": "brand-kit.zip",
             "content_type": "application/zip", "size": 8_612_902,
             "path": "/People Ops", "owner": "brand@acme.example", "tags": []},
        ]
        return folders + files
