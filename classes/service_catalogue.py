import requests
import json
import time
from typing import Any, Dict, List
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse
from utilities.job_log_handling import (
  log_debug,
  log_error,
  log_info,
  log_critical,
  log_warning,
)


def _set_page(url: str, page: int) -> str:
  """Return `url` with pagination[page] set to `page`, preserving existing params."""
  parsed = urlparse(url)
  query = dict(parse_qsl(parsed.query, keep_blank_values=True))
  query['pagination[page]'] = str(page)
  new_query = urlencode(query, doseq=True)
  return urlunparse(parsed._replace(query=new_query))


def _basename(url: str) -> str:
  """Return URL without query string (for compact logging)."""
  return url.split('?', 1)[0]


class ServiceCatalogue:
  def __init__(self, params):
    # default variables
    page_size = 10
    pagination_page_size = f'&pagination[pageSize]={page_size}'
    # Example Sort filter
    # sort_filter='&sort=updatedAt:asc'
    sort_filter = ''

    self.url = params['url']
    self.key = params['key']

    # limit results for testing/dev
    # See strapi filter syntax https://docs.strapi.io/dev-docs/api/rest/filters-locale-publication
    # Example filter string = '&filters[name][$contains]=example'
    self.filter = params.get('filter', '')

    self.product_filter = '&fields[0]=slack_channel_id&fields[1]=slack_channel_name&fields[2]=p_id&fields[3]=name'

    self.api_headers = {
      'Authorization': f'Bearer {self.key}',
      'Content-Type': 'application/json',
      'Accept': 'application/json',
    }
    self.components = 'components'
    self.components_get = f'{self.components}?populate[latest_commit]=true&populate[product]=true&populate[envs]=true{self.filter}{pagination_page_size}{sort_filter}'

    self.products = 'products'
    self.products_get = f'{self.products}?populate[parent]=true&populate[children]=true&populate[product_set]=true&populate[service_area]=true&populate[team]=true{self.product_filter}{pagination_page_size}{sort_filter}'

    self.github_teams = 'github-teams'
    self.environments = 'environments'
    self.environments_get = (
      f'{self.environments}?populate[component]=true{pagination_page_size}{sort_filter}'
    )
    self.scheduled_jobs = 'scheduled-jobs'
    self.connection_ok = self.test_connection()

  """
  Test connection to the Service Catalogue
  """

  def test_connection(self):
    # Test connection to Service Catalogue
    try:
      log_info(f'Testing connection to the Service Catalogue - {self.url}')
      r = requests.head(f'{self.url}', headers=self.api_headers, timeout=10)
      log_info(
        f'Successfully connected to the Service Catalogue - {self.url}. {r.status_code}'
      )
      return True
    except Exception as e:
      log_critical(f'Unable to connect to the Service Catalogue - {e}')
      return False

  """
  Generic get request with retry functionality
  """

  def _request_json_with_retry(
    self,
    url: str,
    max_retries: int,
    timeout: int,
  ) -> Dict[str, Any]:
    """GET JSON with retry/backoff; raises after exhausting retries."""
    attempt = 0
    last_err: Exception | None = None

    while attempt < max_retries:
      try:
        resp = requests.get(url, headers=self.api_headers, timeout=timeout)
        resp.raise_for_status()  # Raises for non-2xx
        return resp.json()
      except (requests.RequestException, ValueError) as e:
        # Request errors or invalid JSON
        last_err = e
        attempt += 1
        log_warning(
          f'Service Catalogue API error for {_basename(url)} '
          f'(attempt {attempt}/{max_retries}): {e}'
        )
        if attempt < max_retries:
          # Exponential backoff: 0.5s, 1.0s, 2.0s...
          time.sleep(0.5 * (2 ** (attempt - 1)))

    # Out of retries
    raise RuntimeError(
      f'Exceeded retries for {_basename(url)} (last error: {last_err})'
    ) from last_err

  def get_with_retry(
    self,
    uri: str,
    max_retries: int = 3,
    timeout: int = 10,
  ) -> List[Any]:
    """
    Fetch all pages for the given `uri`, aggregating the `field` array from each page.
    - Retries each page up to `max_retries` times with exponential backoff.
    - Preserves any existing query params on `uri`.
    """
    base_url = f'{self.url.rstrip("/")}/v1/{uri.lstrip("/")}'
    json_data: List[Any] = []

    # First page
    try:
      first = self._request_json_with_retry(base_url, max_retries, timeout)
      pagination = first['meta']['pagination']
      log_debug(f'Got result page: {pagination["page"]} from Service Catalogue')
      page_count = int(pagination.get('pageCount', 1))
      json_data.extend(first.get('data', []))
    except Exception as e:
      log_error(f'Failed to get page data from Service Catalogue: {e}')
      # If meta/pagination missing, assume single page
      page_count = 1

    # Remaining pages (if any)
    for p in range(2, page_count + 1):
      page_url = _set_page(base_url, p)
      try:
        page_json = self._request_json_with_retry(page_url, max_retries, timeout)
        p_meta = page_json['meta']['pagination']
        log_debug(f'Got result page: {p_meta["page"]} from Service Catalogue')
        json_data.extend(page_json.get('data', []))
      except Exception:
        pass  # If pagination info missing, don't fail aggregation

    return json_data

  """
  Get all multipage results from Service Catalogue
  """

  def get_all_records(self, table):
    log_info(
      f'Getting all records from table {table} in Service Catalogue using URL: {self.url}/v1/{table}'
    )
    return self.get_with_retry(table)

  """
  Get a single record by filter parameter from the Service Catalogue
  """

  def get_record(self, table, label, parameter):
    if '?' in table:  # add an extra parameter if there are already parameters
      filter = f'&filters[{label}][$eq]={parameter}'
    else:
      filter = f'?filters[{label}][$eq]={parameter}'
    if json_data := self.get_with_retry(f'{table}{filter}'):
      return json_data[0]
    else:
      return {}

  """
  Update a record in the Service Catalogue with passed-in JSON data
  """

  def update(self, table, element_id, data):
    success = False
    try:
      log_debug(f'data to be uploaded: {json.dumps(data, indent=2)}')
      x = requests.put(
        f'{self.url}/v1/{table}/{element_id}',
        headers=self.api_headers,
        json={'data': data},
        timeout=10,
      )
      if x.status_code == 200:
        log_info(
          f'Successfully updated record {element_id} in {table.split("/")[-1]}: {x.status_code}'
        )
        success = True
      else:
        log_error(
          f'Received non-200 response from service catalogue for record id {element_id} in {table.split("/")[-1]}: {x.status_code} {x.content}'
        )
    except Exception as e:
      log_error(
        f'Error updating service catalogue for record id {element_id} in {table.split("/")[-1]}: {e}'
      )
    return success

  def add(self, table, data):
    success = False
    try:
      log_debug(data)
      x = requests.post(
        f'{self.url}/v1/{table}',
        headers=self.api_headers,
        json={'data': data},
        timeout=10,
      )
      if x.status_code == 201:
        log_info(
          f'Successfully added {(data["team_name"] if "team_name" in data else data["name"])} to {table.split("/")[-1]}: {x.status_code}'
        )
        success = True
      else:
        log_error(
          f'Received non-201 response from service catalogue to add a record to {table.split("/")[-1]}: {x.status_code} {x.content}'
        )
    except Exception as e:
      log_error(
        f'Error adding a record to {table.split("/")[-1]} in service catalogue: {e}'
      )
    return success

  def delete(self, table, element_id):
    success = False
    try:
      log_debug(f'Deleting record {element_id} from {table.split("/")[-1]}')
      x = requests.delete(
        f'{self.url}/v1/{table}/{element_id}',
        headers=self.api_headers,
        timeout=10,
      )
      if x.status_code == 200:
        log_info(
          f'Successfully deleted record {element_id} from {table.split("/")[-1]}: {x.status_code}'
        )
        success = True
      else:
        log_error(
          f'Received non-200 response from service catalogue deleting record id {element_id} in {table.split("/")[-1]}: {x.status_code} {x.content}'
        )
    except Exception as e:
      log_error(
        f'Error deleting record {element_id} from {table.split("/")[-1]} in service catalogue: {e}'
      )
    return success

  # eg get_id('github-teams', 'team_name', 'example')
  def get_id(self, match_table, match_field, match_string):
    uri = (
      f'{match_table}?filters[{match_field}][$eq]={match_string.replace("&", "&amp;")}'
    )
    if json_data := self.get_with_retry(uri):
      if sc_id := json_data[0].get('documentId'):
        log_debug(
          f'Successfully found Service Catalogue documentID for {match_field}={match_string} in {match_table}: {sc_id}'
        )
        return sc_id
      else:
        log_warning(
          f'Could not find Service Catalogue documentID for {match_field}={match_string} in {match_table}'
        )

  def get_component_env_id(self, component, env):
    env_id = None
    for env in component.get('envs', {}):
      if env.get['name'] == env:
        env_id = env['documentId']
        log_debug(
          f'Found existing environment ID for {env} in component {component.get("name")}: {env_id}'
        )
    if not env_id:
      log_debug(
        f'No existing environment ID found for {env} in component {component.get("name")}'
      )
    return env_id

  def find_all_teams_ref_in_sc(self):
    components = self.get_all_records(self.components_get)
    combined_teams = set()
    for component in components:
      combined_teams.update(component.get('github_project_teams_write', []) or [])
      combined_teams.update(component.get('github_project_teams_admin', []) or [])
      combined_teams.update(component.get('github_project_teams_maintain', []) or [])
    return combined_teams
