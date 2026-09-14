"""Place vocabulary for country detection.

The first live run showed that a hand-picked list of sixty country names is not a country
detector. Postings name a city and nothing else ("Mumbai"), use a two-letter prefix
("FR - Paris"), name a region ("Remote-Iberia"), or name a country that simply was not on
the list ("Croatia"). Each of those was reported as "does not state a country" -- and a
US-only search surfaced them as flags instead of rejecting them.

Everything here is data, and none of it is about any profession. Order inside each dict
matters where one name contains another ("northern ireland" before "ireland", "papua new
guinea" before "guinea"): the first phrase found wins.

Deliberate omissions, each because the US reading is overwhelmingly the intended one in a
job posting and a wrong FAIL silently drops real jobs:

- Georgia (the state), Jersey (New Jersey, Jersey City), Chad and Jordan (people's names),
  Nice (an adjective), and Niger (a substring hazard) are not treated as countries.
- Cities whose bare name is at least as likely to be a US city are left out of the
  non-US gazetteer: Cambridge, Birmingham, Manchester, Vienna, Athens, Rome, Lima, Naples,
  Florence, Geneva, Waterloo, Richmond, Victoria, Hamilton, Kingston, Windsor, Bristol,
  Valencia, San José, Panama City, Wellington. A "City, ST" form still resolves them.
"""

from __future__ import annotations

import re

# ISO 3166-1 names and the aliases postings actually use. Multi-word names that contain a
# shorter name in this dict must precede it.
COUNTRIES: dict[str, str] = {
    # Britain and Ireland
    "united kingdom": "GB", "great britain": "GB", "england": "GB", "scotland": "GB",
    "wales": "GB", "northern ireland": "GB", "britain": "GB", "u.k.": "GB", "uk": "GB",
    "republic of ireland": "IE", "ireland": "IE",
    # Western and Northern Europe
    "germany": "DE", "deutschland": "DE", "france": "FR", "spain": "ES", "españa": "ES",
    "portugal": "PT", "italy": "IT", "italia": "IT", "the netherlands": "NL",
    "netherlands": "NL", "holland": "NL", "belgium": "BE", "luxembourg": "LU",
    "switzerland": "CH", "austria": "AT", "liechtenstein": "LI", "monaco": "MC",
    "sweden": "SE", "norway": "NO", "denmark": "DK", "finland": "FI", "iceland": "IS",
    "faroe islands": "FO", "greenland": "GL", "malta": "MT", "cyprus": "CY",
    "andorra": "AD", "san marino": "SM", "vatican": "VA", "gibraltar": "GI",
    "isle of man": "IM", "guernsey": "GG",
    # Central and Eastern Europe
    "poland": "PL", "czech republic": "CZ", "czechia": "CZ", "slovakia": "SK",
    "hungary": "HU", "romania": "RO", "bulgaria": "BG", "greece": "GR", "croatia": "HR",
    "slovenia": "SI", "serbia": "RS", "bosnia and herzegovina": "BA", "bosnia": "BA",
    "montenegro": "ME", "north macedonia": "MK", "macedonia": "MK", "albania": "AL",
    "kosovo": "XK", "moldova": "MD", "ukraine": "UA", "belarus": "BY", "russia": "RU",
    "russian federation": "RU", "estonia": "EE", "latvia": "LV", "lithuania": "LT",
    "türkiye": "TR", "turkiye": "TR", "turkey": "TR", "armenia": "AM", "azerbaijan": "AZ",
    # Middle East
    "israel": "IL", "united arab emirates": "AE", "uae": "AE", "dubai": "AE",
    "abu dhabi": "AE", "saudi arabia": "SA", "qatar": "QA", "kuwait": "KW", "bahrain": "BH",
    "oman": "OM", "lebanon": "LB", "iraq": "IQ", "iran": "IR", "syria": "SY", "yemen": "YE",
    "palestine": "PS",
    # South and Central Asia
    "india": "IN", "pakistan": "PK", "bangladesh": "BD", "sri lanka": "LK", "nepal": "NP",
    "bhutan": "BT", "maldives": "MV", "afghanistan": "AF", "kazakhstan": "KZ",
    "uzbekistan": "UZ", "kyrgyzstan": "KG", "tajikistan": "TJ", "turkmenistan": "TM",
    "mongolia": "MN",
    # East and Southeast Asia
    "china": "CN", "people's republic of china": "CN", "hong kong": "HK", "macau": "MO",
    "macao": "MO", "taiwan": "TW", "japan": "JP", "south korea": "KR",
    "republic of korea": "KR", "korea": "KR", "north korea": "KP", "singapore": "SG",
    "malaysia": "MY", "indonesia": "ID", "philippines": "PH", "vietnam": "VN",
    "viet nam": "VN", "thailand": "TH", "cambodia": "KH", "laos": "LA", "myanmar": "MM",
    "burma": "MM", "brunei": "BN", "timor-leste": "TL", "east timor": "TL",
    # Oceania
    "australia": "AU", "new zealand": "NZ", "aotearoa": "NZ", "papua new guinea": "PG",
    "fiji": "FJ", "samoa": "WS", "tonga": "TO", "vanuatu": "VU", "solomon islands": "SB",
    "new caledonia": "NC", "french polynesia": "PF",
    # Americas (non-US)
    "canada": "CA", "mexico": "MX", "méxico": "MX", "brazil": "BR", "brasil": "BR",
    "argentina": "AR", "chile": "CL", "colombia": "CO", "peru": "PE", "perú": "PE",
    "venezuela": "VE", "ecuador": "EC", "bolivia": "BO", "paraguay": "PY", "uruguay": "UY",
    "guyana": "GY", "suriname": "SR", "french guiana": "GF", "costa rica": "CR",
    "panama": "PA", "panamá": "PA", "guatemala": "GT", "honduras": "HN",
    "el salvador": "SV", "nicaragua": "NI", "belize": "BZ", "cuba": "CU",
    "dominican republic": "DO", "dominica": "DM", "haiti": "HT", "jamaica": "JM",
    "trinidad and tobago": "TT", "trinidad": "TT", "barbados": "BB", "bahamas": "BS",
    "bermuda": "BM", "cayman islands": "KY", "saint lucia": "LC", "grenada": "GD",
    "antigua and barbuda": "AG", "saint kitts and nevis": "KN",
    "saint vincent and the grenadines": "VC", "aruba": "AW", "curaçao": "CW", "curacao": "CW",
    "british virgin islands": "VG", "turks and caicos": "TC", "martinique": "MQ",
    "guadeloupe": "GP",
    # Africa
    "south africa": "ZA", "nigeria": "NG", "kenya": "KE", "egypt": "EG", "morocco": "MA",
    "algeria": "DZ", "tunisia": "TN", "libya": "LY", "ethiopia": "ET", "ghana": "GH",
    "tanzania": "TZ", "uganda": "UG", "rwanda": "RW", "senegal": "SN", "ivory coast": "CI",
    "côte d'ivoire": "CI", "cote d'ivoire": "CI", "cameroon": "CM", "angola": "AO",
    "mozambique": "MZ", "zambia": "ZM", "zimbabwe": "ZW", "botswana": "BW", "namibia": "NA",
    "mauritius": "MU", "madagascar": "MG", "malawi": "MW", "mali": "ML",
    "burkina faso": "BF", "sudan": "SD", "south sudan": "SS", "somalia": "SO",
    "eritrea": "ER", "djibouti": "DJ", "democratic republic of the congo": "CD",
    "dr congo": "CD", "republic of the congo": "CG", "congo": "CG", "gabon": "GA",
    "equatorial guinea": "GQ", "guinea-bissau": "GW", "guinea": "GN", "sierra leone": "SL",
    "liberia": "LR", "togo": "TG", "benin": "BJ", "mauritania": "MR", "gambia": "GM",
    "cape verde": "CV", "cabo verde": "CV", "seychelles": "SC", "comoros": "KM",
    "lesotho": "LS", "eswatini": "SZ", "swaziland": "SZ", "burundi": "BI",
    "central african republic": "CF", "são tomé and príncipe": "ST",
}

# Region shorthands are not countries, but they establish non-US scope just as firmly.
# The code is a label, not ISO; the country gate only asks "is it US".
REGIONS: dict[str, str] = {
    "emea": "EMEA", "apac": "APAC", "latam": "LATAM", "iberia": "ES", "nordics": "EU",
    "benelux": "EU", "dach": "EU", "anz": "AU", "mena": "MENA", "uk&i": "GB", "uki": "GB",
    "european union": "EU", "europe": "EU", "asia pacific": "APAC", "asia-pacific": "APAC",
    "southeast asia": "APAC", "south east asia": "APAC", "latin america": "LATAM",
    "south america": "LATAM", "central america": "LATAM", "middle east": "MENA",
    "sub-saharan africa": "AFRICA", "africa": "AFRICA", "oceania": "APAC",
    "scandinavia": "EU", "baltics": "EU", "balkans": "EU", "caribbean": "LATAM",
    "canada-wide": "CA", "across canada": "CA", "gcc": "MENA",
    # Sub-national regions that name their country as firmly as the country would.
    "ontario": "CA", "british columbia": "CA", "alberta": "CA", "quebec": "CA", "québec": "CA",
    "manitoba": "CA", "saskatchewan": "CA", "nova scotia": "CA", "newfoundland": "CA",
    "new south wales": "AU", "queensland": "AU", "tasmania": "AU",
    "greater london": "GB", "west midlands": "GB", "greater manchester": "GB",
    "bavaria": "DE", "bayern": "DE", "baden-württemberg": "DE", "north rhine-westphalia": "DE",
    "hesse": "DE", "hessen": "DE", "karnataka": "IN", "maharashtra": "IN", "tamil nadu": "IN",
    "telangana": "IN", "haryana": "IN", "gujarat": "IN", "kerala": "IN", "west bengal": "IN",
    "uttar pradesh": "IN", "rajasthan": "IN", "punjab": "IN", "catalonia": "ES",
    "andalusia": "ES", "lombardy": "IT", "île-de-france": "FR", "ile-de-france": "FR",
}

# Major non-US metros whose bare name is unambiguous in a job posting. A "City, ST" form
# is resolved before this list is consulted, so "Paris, TX" is Texas and "Paris" is France.
CITIES: dict[str, str] = {
    # United Kingdom and Ireland
    "london": "GB", "edinburgh": "GB", "glasgow": "GB", "belfast": "GB", "leeds": "GB",
    "cardiff": "GB", "liverpool": "GB", "sheffield": "GB", "newcastle upon tyne": "GB",
    "nottingham": "GB", "leicester": "GB", "milton keynes": "GB",
    "dublin": "IE", "cork": "IE", "galway": "IE", "limerick": "IE",
    # Western and Northern Europe
    "paris": "FR", "lyon": "FR", "toulouse": "FR", "marseille": "FR", "bordeaux": "FR",
    "lille": "FR", "nantes": "FR", "strasbourg": "FR", "sophia antipolis": "FR",
    "berlin": "DE", "munich": "DE", "münchen": "DE", "hamburg": "DE", "frankfurt": "DE",
    "cologne": "DE", "köln": "DE", "düsseldorf": "DE", "dusseldorf": "DE", "stuttgart": "DE",
    "leipzig": "DE", "dresden": "DE", "nuremberg": "DE", "nürnberg": "DE", "hannover": "DE",
    "hanover": "DE", "bremen": "DE", "dortmund": "DE", "essen": "DE", "karlsruhe": "DE",
    "heidelberg": "DE", "mannheim": "DE", "bonn": "DE",
    "amsterdam": "NL", "rotterdam": "NL", "the hague": "NL", "utrecht": "NL",
    "eindhoven": "NL", "brussels": "BE", "antwerp": "BE", "ghent": "BE", "leuven": "BE",
    "zurich": "CH", "zürich": "CH", "lausanne": "CH", "basel": "CH", "bern": "CH",
    "madrid": "ES", "barcelona": "ES", "seville": "ES", "sevilla": "ES", "bilbao": "ES",
    "málaga": "ES", "malaga": "ES", "lisbon": "PT", "lisboa": "PT", "porto": "PT",
    "milan": "IT", "milano": "IT", "turin": "IT", "torino": "IT", "bologna": "IT",
    "stockholm": "SE", "gothenburg": "SE", "göteborg": "SE", "malmö": "SE", "malmo": "SE",
    "copenhagen": "DK", "københavn": "DK", "aarhus": "DK", "oslo": "NO", "bergen": "NO",
    "helsinki": "FI", "tampere": "FI", "espoo": "FI", "reykjavik": "IS", "reykjavík": "IS",
    "graz": "AT", "linz": "AT", "salzburg": "AT",
    # Central and Eastern Europe
    "warsaw": "PL", "warszawa": "PL", "krakow": "PL", "kraków": "PL", "wroclaw": "PL",
    "wrocław": "PL", "gdansk": "PL", "gdańsk": "PL", "poznan": "PL", "poznań": "PL",
    "prague": "CZ", "praha": "CZ", "brno": "CZ", "bratislava": "SK", "budapest": "HU",
    "bucharest": "RO", "bucurești": "RO", "cluj-napoca": "RO", "cluj": "RO", "sofia": "BG",
    "zagreb": "HR", "ljubljana": "SI", "belgrade": "RS", "beograd": "RS", "tallinn": "EE",
    "riga": "LV", "vilnius": "LT", "kyiv": "UA", "kiev": "UA", "lviv": "UA",
    "thessaloniki": "GR", "istanbul": "TR", "ankara": "TR", "izmir": "TR", "tbilisi": "GE",
    "yerevan": "AM", "baku": "AZ", "minsk": "BY", "moscow": "RU",
    "nicosia": "CY", "limassol": "CY", "valletta": "MT",
    # Middle East
    "tel aviv": "IL", "tel-aviv": "IL", "jerusalem": "IL", "haifa": "IL", "herzliya": "IL",
    "ramat gan": "IL", "petah tikva": "IL", "riyadh": "SA", "jeddah": "SA", "doha": "QA",
    "kuwait city": "KW", "manama": "BH", "muscat": "OM", "amman": "JO", "beirut": "LB",
    "tehran": "IR", "baghdad": "IQ",
    # South Asia
    "mumbai": "IN", "bombay": "IN", "bangalore": "IN", "bengaluru": "IN", "hyderabad": "IN",
    "chennai": "IN", "pune": "IN", "delhi": "IN", "new delhi": "IN", "gurgaon": "IN",
    "gurugram": "IN", "noida": "IN", "kolkata": "IN", "ahmedabad": "IN", "kochi": "IN",
    "chandigarh": "IN", "jaipur": "IN", "indore": "IN", "coimbatore": "IN",
    "thiruvananthapuram": "IN", "trivandrum": "IN", "karachi": "PK", "lahore": "PK",
    "islamabad": "PK", "dhaka": "BD", "colombo": "LK", "kathmandu": "NP",
    # East and Southeast Asia
    "tokyo": "JP", "osaka": "JP", "nagoya": "JP", "fukuoka": "JP", "yokohama": "JP",
    "kyoto": "JP", "seoul": "KR", "busan": "KR", "shanghai": "CN", "beijing": "CN",
    "shenzhen": "CN", "guangzhou": "CN", "hangzhou": "CN", "chengdu": "CN", "wuhan": "CN",
    "nanjing": "CN", "suzhou": "CN", "chongqing": "CN", "tianjin": "CN", "xi'an": "CN",
    "taipei": "TW", "taichung": "TW", "kaohsiung": "TW", "hsinchu": "TW", "manila": "PH",
    "makati": "PH", "cebu": "PH", "jakarta": "ID", "bandung": "ID", "kuala lumpur": "MY",
    "penang": "MY", "bangkok": "TH", "ho chi minh city": "VN", "hanoi": "VN",
    "da nang": "VN", "phnom penh": "KH", "yangon": "MM",
    # Oceania
    "sydney": "AU", "melbourne": "AU", "brisbane": "AU", "perth": "AU", "adelaide": "AU",
    "canberra": "AU", "gold coast": "AU", "hobart": "AU", "auckland": "NZ",
    "christchurch": "NZ",
    # Canada
    "toronto": "CA", "montreal": "CA", "montréal": "CA", "vancouver": "CA", "calgary": "CA",
    "ottawa": "CA", "edmonton": "CA", "winnipeg": "CA", "quebec city": "CA",
    "québec city": "CA", "halifax": "CA", "mississauga": "CA", "brampton": "CA",
    "markham": "CA", "kitchener": "CA", "saskatoon": "CA", "surrey": "CA", "burnaby": "CA",
    # Latin America
    "mexico city": "MX", "ciudad de méxico": "MX", "cdmx": "MX", "guadalajara": "MX",
    "monterrey": "MX", "tijuana": "MX", "são paulo": "BR", "sao paulo": "BR",
    "rio de janeiro": "BR", "belo horizonte": "BR", "curitiba": "BR", "porto alegre": "BR",
    "florianópolis": "BR", "florianopolis": "BR", "recife": "BR", "campinas": "BR",
    "buenos aires": "AR", "córdoba": "AR", "cordoba": "AR", "rosario": "AR",
    "santiago": "CL", "bogotá": "CO", "bogota": "CO", "medellín": "CO", "medellin": "CO",
    "quito": "EC", "guayaquil": "EC", "montevideo": "UY", "asunción": "PY",
    "asuncion": "PY", "la paz": "BO", "caracas": "VE", "havana": "CU",
    "santo domingo": "DO", "san salvador": "SV", "tegucigalpa": "HN", "managua": "NI",
    # Africa
    "cairo": "EG", "nairobi": "KE", "lagos": "NG", "abuja": "NG",
    "johannesburg": "ZA", "cape town": "ZA", "durban": "ZA", "pretoria": "ZA",
    "casablanca": "MA", "rabat": "MA", "tunis": "TN", "algiers": "DZ", "accra": "GH",
    "addis ababa": "ET", "kampala": "UG", "dar es salaam": "TZ", "kigali": "RW",
    "lusaka": "ZM", "harare": "ZW", "dakar": "SN", "abidjan": "CI",
}

# US cities whose bare name establishes US scope. Used only after every non-US signal has
# been checked, so a posting reading "Paris or Austin" is caught by "Paris" first -- which
# is the safe error for a US-only gate.
US_CITIES: list[str] = [
    "new york city", "nyc", "san francisco", "los angeles", "chicago", "houston", "phoenix",
    "philadelphia", "san antonio", "san diego", "dallas", "san jose", "austin",
    "jacksonville", "fort worth", "columbus", "charlotte", "indianapolis", "seattle",
    "denver", "washington, d.c.", "washington dc", "boston", "el paso", "nashville",
    "detroit", "oklahoma city", "portland", "las vegas", "memphis", "louisville",
    "baltimore", "milwaukee", "albuquerque", "tucson", "fresno", "sacramento",
    "kansas city", "mesa", "atlanta", "omaha", "colorado springs", "raleigh", "miami",
    "long beach", "virginia beach", "oakland", "minneapolis", "tulsa", "tampa",
    "arlington", "new orleans", "wichita", "cleveland", "bakersfield", "aurora",
    "anaheim", "honolulu", "santa ana", "riverside", "corpus christi", "lexington",
    "henderson", "stockton", "saint paul", "st. paul", "cincinnati", "st. louis",
    "saint louis", "pittsburgh", "greensboro", "lincoln", "anchorage", "plano",
    "orlando", "irvine", "newark", "durham", "chula vista", "toledo", "fort wayne",
    "laredo", "jersey city", "chandler", "madison", "lubbock",
    "scottsdale", "reno", "buffalo", "gilbert", "glendale", "north las vegas",
    "winston-salem", "chesapeake", "norfolk", "fremont", "garland", "irving", "hialeah",
    "boise", "spokane", "baton rouge", "tacoma", "san bernardino", "modesto", "fontana",
    "des moines", "moreno valley", "santa clarita", "fayetteville", "oxnard", "rochester",
    "port st. lucie", "grand rapids", "huntsville", "salt lake city", "frisco", "yonkers",
    "amarillo", "huntington beach", "mckinney",
    "montgomery", "augusta", "akron", "little rock", "tempe", "overland park",
    "grand prairie", "tallahassee", "cape coral", "knoxville", "shreveport",
    "worcester", "vancouver, wa", "sioux falls", "chattanooga", "brownsville",
    "fort lauderdale", "providence", "newport news", "rancho cucamonga", "santa rosa",
    "peoria", "oceanside", "elk grove", "salem", "pembroke pines", "eugene", "garden grove",
    "fort collins", "corona", "springfield", "jackson", "alexandria",
    "hayward", "clarksville", "lakewood", "lancaster", "salinas", "palmdale", "hollywood",
    "pasadena", "sunnyvale", "macon", "pomona", "escondido", "killeen", "naperville",
    "joliet", "bellevue", "mountain view", "palo alto", "menlo park", "redwood city",
    "san mateo", "santa clara", "cupertino", "redmond", "kirkland", "bothell",
    "silicon valley", "bay area", "sf bay area", "greater boston", "dc metro",
    "research triangle", "twin cities", "dfw", "dallas-fort worth", "dallas/fort worth",
    "south florida", "southern california", "socal", "norcal", "new england",
    "pacific northwest", "midwest", "east coast", "west coast", "tri-state area",
]


# ISO alpha-2 codes that are also US state abbreviations. A bare "CA - <unknown city>" is
# California to one reader and Canada to another; without a recognisable city it is
# reported as unknown rather than guessed.
AMBIGUOUS_PREFIXES: frozenset[str] = frozenset({
    "AL", "AR", "CA", "CO", "DE", "GA", "ID", "IL", "IN", "KY", "LA", "MA", "MD", "ME",
    "MN", "MO", "MS", "MT", "NC", "NE", "PA", "SC", "SD", "TN", "VA",
})

PREFIX_ALIASES: dict[str, str] = {"UK": "GB"}


class Vocabulary:
    """A name-to-code lookup compiled to one pattern.

    Compiling once matters: the first version searched every name with its own pattern,
    which at eight hundred names and seventeen thousand postings is minutes of regex per
    run. Names are ordered longest first so that, at the same position, the alternation
    prefers "northern ireland" to "ireland".
    """

    def __init__(self, names: dict[str, str] | list[str], code: str | None = None):
        mapping = names if isinstance(names, dict) else {n: code for n in names}
        self._codes = {" ".join(k.lower().split()): v for k, v in mapping.items()}
        ordered = sorted(self._codes, key=len, reverse=True)
        alternatives = "|".join(r"[\s\-/]+".join(re.escape(w) for w in n.split())
                                for n in ordered)
        self._pattern = re.compile(rf"(?<!\w)(?:{alternatives})(?!\w)", re.IGNORECASE)

    def find(self, text: str) -> tuple[str, tuple[int, int]] | None:
        match = self._pattern.search(text)
        if not match:
            return None
        key = " ".join(match.group(0).lower().split())
        # Flexible whitespace and hyphens mean the matched text may not be the key
        # verbatim; normalise the separators the same way before looking it up.
        key = re.sub(r"[\-/]+", " ", key)
        code = self._codes.get(key) or self._codes.get(key.replace(" ", "-"))
        if code is None:
            # Defensive: find the key whose words match, separator-insensitively.
            for candidate, value in self._codes.items():
                if re.sub(r"[\s\-/]+", " ", candidate) == key:
                    code = value
                    break
        return (code, match.span()) if code else None


# One vocabulary for everything non-US, so that leftmost-longest is honoured across the
# three lists: "New South Wales" must be read as the Australian state at position 0, not
# as "Wales" at position 10.
NON_US_VOCAB = Vocabulary({**COUNTRIES, **REGIONS, **CITIES})
US_CITY_VOCAB = Vocabulary(US_CITIES, "US")
ISO_CODES: frozenset[str] = frozenset(COUNTRIES.values())
