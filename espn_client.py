import time

import requests

SITE_BASE = "https://site.api.espn.com/apis/site/v2/sports/football"

LEAGUES = {
    "cfb": {"path": "college-football", "scoreboard_params": {"groups": 80, "limit": 500}},
    "nfl": {"path": "nfl", "scoreboard_params": {"limit": 100}},
}

RETRYABLE_STATUSES = {429, 502, 503, 504}


class EspnClient:
    def __init__(self, league, max_attempts=5, timeout=(10, 60)):
        if league not in LEAGUES:
            raise ValueError(f"Unknown league {league!r}; expected one of {list(LEAGUES)}")
        self.league = league
        self.config = LEAGUES[league]
        self.base_url = f"{SITE_BASE}/{self.config['path']}"
        self.max_attempts = max_attempts
        self.timeout = timeout
        self.session = requests.Session()

    def get(self, endpoint, params=None):
        url = f"{self.base_url}/{endpoint.lstrip('/')}"

        for attempt in range(1, self.max_attempts + 1):
            try:
                response = self.session.get(url, params=params, timeout=self.timeout)
            except (requests.ConnectionError, requests.Timeout) as exc:
                if attempt == self.max_attempts:
                    raise
                delay = 5 * attempt
                reason = type(exc).__name__
            else:
                if response.status_code not in RETRYABLE_STATUSES or attempt == self.max_attempts:
                    response.raise_for_status()
                    return response.json()
                delay = self._retry_delay(response, attempt)
                reason = f"HTTP {response.status_code}"

            print(
                f"[{self.league}] {endpoint}: {reason}; retrying in {delay}s "
                f"(attempt {attempt + 1}/{self.max_attempts})",
                flush=True,
            )
            time.sleep(delay)

    @staticmethod
    def _retry_delay(response, attempt):
        retry_after = response.headers.get("Retry-After")
        if retry_after is not None:
            try:
                return max(float(retry_after), 5)
            except ValueError:
                pass
        return 15 * attempt if response.status_code == 429 else 5 * attempt

    def fetch_teams(self):
        return self.get("teams", params={"limit": 10000})

    def fetch_team(self, team_id):
        return self.get(f"teams/{team_id}")

    def fetch_games(self, year, week, season_type=2, **overrides):
        params = {
            "dates": year,
            "seasontype": season_type,
            "week": week,
            **self.config["scoreboard_params"],
            **overrides,
        }
        return self.get("scoreboard", params=params)
