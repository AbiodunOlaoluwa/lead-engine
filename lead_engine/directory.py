"""A fictional property-firm directory, generated deterministically, with the mess real directories have.

Three sources describe overlapping firms:
- portal/     listing cards in two layouts (standard and "premium"), some firms on two accounts, some with
              free-text profile pages that only an extractor can read
- register/   a professional register: names SURNAME FIRST in capitals, firm names with suffix noise,
              phones that lost their leading 0 or hold two numbers, no emails
- robots.txt  a crawl delay and a disallowed path

Everything is fictional: .example domains and phone numbers in an unassigned 0800 555 range.
truth.json records which firm each listing really belongs to, so matching can be scored end to end.
"""
from __future__ import annotations

import html
import json
import random
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

FIRST = [
    ("Olufunmilayo", "Funmi"), ("Babatunde", "Tunde"), ("Oluwatoyin", "Toyin"), ("Chukwuemeka", "Emeka"),
    ("Adebayo", "Bayo"), ("Oluwaseun", "Seun"), ("Ngozi", None), ("Ibrahim", None), ("Temitope", "Tope"),
    ("Chinedu", "Nedu"), ("Abimbola", "Bimbo"), ("Olanrewaju", "Lanre"), ("Aishat", None), ("Oluwafemi", "Femi"),
    ("Uchenna", "Uche"), ("Kemisola", "Kemi"), ("Damilola", "Dami"), ("Obinna", "Obi"), ("Bukola", "Bukky"),
    ("Ayodeji", "Deji"), ("Hauwa", None), ("Ifeoma", None), ("Tobiloba", "Tobi"), ("Adewale", "Wale"),
]
MIDDLE = ["Oluwaseun", "Adeola", "Chiamaka", "Ibukun", "Nkechi", "Abiola", "Emeka", "Folake", "Tolu", "Kelechi"]
SURNAMES = [
    "Adeyemi", "Okafor", "Balogun", "Eze", "Ogunleye", "Nwosu", "Bello", "Okonkwo", "Adebisi", "Ibekwe", "Lawal",
    "Afolabi", "Chukwu", "Danjuma", "Oyelaran", "Uzor", "Akinola", "Nnadi", "Oduya", "Ekwueme", "Amadi", "Salami",
]
COMPANY_FIRST = [
    "Palmview", "Harbour Court", "Northgate", "Lekki Crest", "Greenfield", "Cedar Point", "Riverside", "Mainland",
    "Bluegate", "Oakridge", "Silverline", "Sunrise", "Goldbrook", "Kingsway", "Royal Palm", "Westfield", "Eastgate",
    "Crestwood", "Brookstone", "Ashwood", "Marina Bay", "Highbury", "Pinecrest", "Lagoon View",
]
COMPANY_SECOND = ["Properties", "Homes", "Estates", "Realty", "Property Management", "Facility Management", "Lettings", "Real Estate"]
CITIES = ["Lagos", "Abuja", "Port Harcourt", "Ibadan", "Enugu"]
SERVICES = {
    "management": ["Property management", "Rent collection", "Facility management", "Tenant screening", "Service charge administration"],
    "letting": ["Letting", "Short-let management", "Tenant sourcing"],
    "sales": ["Property sales", "Land sales", "Valuation", "Off-plan sales"],
}


@dataclass
class Firm:
    id: str
    company: str
    first: str
    short: str | None
    middle: str
    surname: str
    city: str
    phones: list[str]
    email: str | None
    domain: str
    for_rent: int
    short_let: int
    for_sale: int
    services: list[str]
    licensed: bool
    member_no: str | None
    portal_accounts: int = 1
    has_profile: bool = False
    premium: bool = False
    listing_ids: list[str] = field(default_factory=list)


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def profile_slug(f: "Firm") -> str:
    # Unique even when two firms share a name.
    return f"{slug(f.company)}-{f.id.lower()}"


def build_firms(n: int = 80, seed: int = 7) -> list[Firm]:
    rnd = random.Random(seed)
    firms: list[Firm] = []
    used_names: set[str] = set()
    for i in range(n):
        while True:
            company = f"{rnd.choice(COMPANY_FIRST)} {rnd.choice(COMPANY_SECOND)}"
            if company not in used_names or rnd.random() < 0.04:  # a few genuine name clashes in different cities
                break
        used_names.add(company)
        first, short = rnd.choice(FIRST)
        kind = rnd.choices(["management", "letting", "sales"], weights=[4, 3, 3])[0]
        for_rent = rnd.randint(15, 130) if kind == "management" else rnd.randint(8, 50) if kind == "letting" else rnd.randint(0, 6)
        short_let = rnd.randint(6, 60) if kind == "letting" else rnd.randint(0, 4)
        for_sale = rnd.randint(30, 90) if kind == "sales" else rnd.randint(0, 12)
        services = rnd.sample(SERVICES[kind], k=2) + rnd.sample(SERVICES["sales"], k=1 if kind != "sales" else 0)
        domain = slug(company).replace("-", "") + ".example"
        phones = [f"0800 555 {1000 + i:04d}"]
        if rnd.random() < 0.3:
            phones.append(f"0809 555 {2000 + i:04d}")
        licensed = rnd.random() < 0.45
        firms.append(
            Firm(
                id=f"F{i:03d}",
                company=company,
                first=first,
                short=short if rnd.random() < 0.6 else None,
                middle=rnd.choice(MIDDLE),
                surname=rnd.choice(SURNAMES),
                city=rnd.choice(CITIES),
                phones=phones,
                email=f"{rnd.choice(['info', 'hello', 'enquiries', first.lower()])}@{domain}" if rnd.random() < 0.55 else None,
                domain=domain,
                for_rent=for_rent,
                short_let=short_let,
                for_sale=for_sale,
                services=services,
                licensed=licensed,
                member_no=f"M{4000 + i}" if licensed else None,
                portal_accounts=2 if rnd.random() < 0.08 else 1,
                has_profile=rnd.random() < 0.3,
                premium=rnd.random() < 0.3,
            )
        )
    return firms


def _card(f: Firm, account: int, listing_id: str) -> str:
    name = f"{f.short or f.first} {f.surname}"
    phone = f.phones[0] if account == 1 else (f.phones[1] if len(f.phones) > 1 else f"0807 555 {3000 + int(f.id[1:]):04d}")
    profile = f'<a class="profile" href="profile/{profile_slug(f)}.html">View profile</a>' if f.has_profile else ""
    email = f'<dd class="email">{html.escape(f.email)}</dd>' if f.email and account == 1 else ""
    if f.premium:
        # The premium layout: no rank prefix, different classes. A parser written only for standard cards skips these silently.
        return f"""
<div class="listing listing--premium" data-id="{listing_id}">
  <h3>{html.escape(f.company)}</h3>
  <span class="agent">Agent: {html.escape(name)}</span>
  <span class="tel">Call: {phone}</span>{f'<span class="mail">{html.escape(f.email)}</span>' if f.email and account == 1 else ''}
  <span class="loc">{f.city}</span>
  <ul class="counts"><li data-k="rent">{f.for_rent}</li><li data-k="short">{f.short_let}</li><li data-k="sale">{f.for_sale}</li></ul>
  <p class="about">{html.escape(', '.join(f.services))}</p>{profile}
</div>"""
    return f"""
<article class="listing" data-id="{listing_id}">
  <h3><span class="rank">{listing_id[-3:]} | </span>{html.escape(f.company)}</h3>
  <dl>
    <dt>Contact</dt><dd class="contact">{html.escape(name)}</dd>
    <dt>Phone</dt><dd class="phone">{phone}</dd>
    {f'<dt>Email</dt>{email}' if email else ''}
    <dt>City</dt><dd class="city">{f.city}</dd>
    <dt>For rent</dt><dd class="rent">{f.for_rent}</dd>
    <dt>Short let</dt><dd class="short">{f.short_let}</dd>
    <dt>For sale</dt><dd class="sale">{f.for_sale}</dd>
    <dt>Services</dt><dd class="services">{html.escape(', '.join(f.services))}</dd>
  </dl>{profile}
</article>"""


def _profile(f: Firm) -> str:
    """Free text, the way firms actually write their 'About' pages. Only an extractor can read it."""
    email = (f.email or f"enquiries@{f.domain}").replace("@", " [at] ").replace(".example", " (dot) example")
    phone = f.phones[-1]
    lead = f"{'ESV ' if f.licensed else ''}{f.first} {f.middle[0]}. {f.surname}"
    return f"""<!doctype html><html><body><main class="profile-page">
<h1>{html.escape(f.company)}</h1>
<p>Founded in {2005 + int(f.id[1:]) % 15}, {html.escape(f.company)} looks after homes across {f.city} for landlords who live abroad.
Our principal partner, {html.escape(lead)}, leads a team that handles {html.escape(', '.join(s.lower() for s in f.services))}.</p>
<p>We are based off Aliyu Turaki House, Adeola Odeku Street. For enquiries, reach the office on {phone}, or write to us at {email}.</p>
<p>Opening hours: Monday to Friday, 9am to 5pm.</p>
</main></body></html>"""


def generate(out: Path, n: int = 80, seed: int = 7) -> dict:
    firms = build_firms(n, seed)
    portal = out / "portal"
    (portal / "profile").mkdir(parents=True, exist_ok=True)
    (out / "register").mkdir(parents=True, exist_ok=True)
    (out / "private").mkdir(parents=True, exist_ok=True)
    (out / "robots.txt").write_text("User-agent: *\nCrawl-delay: 1\nDisallow: /private/\n")
    (out / "private" / "admin.html").write_text("<p>Not for crawlers.</p>")

    truth: dict[str, str] = {}
    cards: list[str] = []
    for f in firms:
        for account in range(1, f.portal_accounts + 1):
            lid = f"portal-{f.id}-{account}"
            truth[lid] = f.id
            f.listing_ids.append(lid)
            cards.append(_card(f, account, lid))
        if f.has_profile:
            pid = f"profile-{profile_slug(f)}"
            truth[pid] = f.id
            f.listing_ids.append(pid)
            (portal / "profile" / f"{profile_slug(f)}.html").write_text(_profile(f))

    per_page = 10
    pages = [cards[i : i + per_page] for i in range(0, len(cards), per_page)]
    links = "".join(f'<li><a href="page-{k + 1}.html">Page {k + 1}</a></li>' for k in range(len(pages)))
    (portal / "index.html").write_text(f"<!doctype html><html><body><h1>Find an agent</h1><ul class='pages'>{links}</ul></body></html>")
    for k, page in enumerate(pages):
        (portal / f"page-{k + 1}.html").write_text(f"<!doctype html><html><body><main>{''.join(page)}</main></body></html>")

    rows = []
    for f in [f for f in firms if f.licensed]:
        rid = f"register-{f.member_no}"
        truth[rid] = f.id
        f.listing_ids.append(rid)
        phones = " / ".join(f.phones)
        if int(f.id[1:]) % 3 == 0:
            phones = re.sub(r"^0", "", phones)  # the export drops leading zeros
        firm_name = f.company.upper() + (" LTD" if int(f.id[1:]) % 2 else " NIG. LIMITED")
        person = f"{f.surname} {f.first} {f.middle}".upper()  # surname first, as registers write it
        rows.append(f"<tr data-id='{rid}'><td>{f.member_no}</td><td>{person}</td><td>{html.escape(firm_name)}</td><td>{phones}</td><td>{f.city.upper()}</td></tr>")
    half = (len(rows) + 1) // 2
    for k, chunk in enumerate([rows[:half], rows[half:]]):
        (out / "register" / f"page-{k + 1}.html").write_text(
            "<!doctype html><html><body><table class='members'><tr><th>No.</th><th>Name</th><th>Firm</th><th>Tel</th><th>State</th></tr>"
            + "".join(chunk)
            + "</table></body></html>"
        )
    (out / "register" / "index.html").write_text(
        "<!doctype html><html><body><a href='page-1.html'>1</a> <a href='page-2.html'>2</a></body></html>"
    )
    meta = {"truth": truth, "firms": [asdict(f) for f in firms]}
    (out / "truth.json").write_text(json.dumps(meta, indent=1))
    return meta
