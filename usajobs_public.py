import os, httpx
from datetime import datetime, timezone

OK_STATES = ["new jersey","new york","connecticut","district of columbia",
             "maryland","virginia","pennsylvania"]
def fed_loc_ok(locstr):
    l = locstr.lower()
    if "anywhere in the u.s" in l or "remote job" in l:
        return True
    return any(s in l for s in OK_STATES)

SERIES = "0343;2210;1910;0301;0685;1101;0501;0360"
API = "https://data.usajobs.gov/api/search"

class FedJob:
    __slots__ = ("url","title","company","location","posted_at","is_remote",
                 "description","raw")

def fetch_public_federal(max_pages=6):
    key = os.environ.get("USAJOBS_API_KEY")
    ua  = os.environ.get("USAJOBS_USER_AGENT")
    if not key or not ua:
        print("[skip] usajobs: env vars not set"); return []
    hdrs = {"Host":"data.usajobs.gov","User-Agent":ua,"Authorization-Key":key}
    out = []
    with httpx.Client(timeout=30, headers=hdrs) as c:
        for page in range(1, max_pages+1):
            p = {"JobCategoryCode":SERIES,"HiringPath":"public",
                 "ResultsPerPage":500,"Page":page}
            try:
                r = c.get(API, params=p)
                if r.status_code != 200:
                    print(f"[skip] usajobs p{page}: {r.status_code}"); break
                items = r.json()["SearchResult"]["SearchResultItems"]
            except Exception as e:
                print(f"[skip] usajobs p{page}: {str(e)[:50]}"); break
            if not items: break
            for it in items:
                d = it["MatchedObjectDescriptor"]
                det = d.get("UserArea",{}).get("Details",{})
                who = (det.get("WhoMayApply",{}) or {}).get("Name","")
                if who and "public" not in who.lower() and "all us citizens" not in who.lower():
                    continue
                j = FedJob()
                j.url = d.get("PositionURI","")
                j.title = d.get("PositionTitle","")
                j.company = (d.get("OrganizationName") or "federal")[:40]
                locs = d.get("PositionLocation") or []
                j.location = "; ".join(l.get("LocationName","") for l in locs)[:120]
                if not fed_loc_ok(j.location):
                    continue
                j.is_remote = ("remote job" in j.location.lower() or
                               "anywhere in the u.s" in j.location.lower())
                try:
                    j.posted_at = datetime.fromisoformat(
                        d["PublicationStartDate"]).replace(tzinfo=timezone.utc)
                except Exception:
                    j.posted_at = None
                j.description = (d.get("QualificationSummary") or "") + " " + \
                    str(det.get("JobSummary",""))
                j.raw = {"JobCategory": d.get("JobCategory") or [],
                         "WhoMayApply": who}
                out.append(j)
            if len(items) < 500: break
    print(f"[ok] usajobs-public: {len(out)}")
    return out
