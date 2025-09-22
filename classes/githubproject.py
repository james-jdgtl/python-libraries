import sys
import os
import logging
import json
import requests
from time import sleep
from github import Github
from github import GithubException

log_level = os.getenv('LOG_LEVEL', 'INFO')
logging.basicConfig(
  format='[%(asctime)s] %(levelname)s %(threadName)s %(message)s', level=log_level
)
log = logging.getLogger(__name__)


class GithubProject:
  def __init__(self, github_params):
    try:
      self.github_bootstrap_repo = github_params['github_bootstrap_repo']
      self.github_org = github_params['github_org']
      self.github_access_token = github_params['github_access_token']
      self.session = Github(self.github_access_token)
      self.org = self.session.get_organization(self.github_org)
      self.bootstrap_repo = self.session.get_repo(
        f'{self.github_org}/{self.github_bootstrap_repo}'
      )
      log.debug(
        f'Initialised GithubProject - bootstrap repo is {self.bootstrap_repo.name}'
      )

    except Exception as e:
      log.error(f'Unable to initialise Github session: {e}')
      sys.exit(1)

  def get_teams(self):
    try:
      self.teams = self.org.get_teams()
      self.team_slugs = {team.slug for team in self.teams}
      log.debug(f'Loaded list of {len(self.team_slugs)} team slugs')
      return True
    except Exception as e:
      log.error(f'Unable to load github teams because: {e}')
      return None

  def create_update_pr(self, request):
    branch_name = f'REQ_{request["id"]}_{request.get("github_repo")}'

    # If the branch doesn't exist - create it
    # This will obviously create a new PR even if one already exists
    all_branches = self.bootstrap_repo.get_branches()
    if branch_name not in [branch.name for branch in all_branches]:
      log.info(f'Branch {branch_name} not found - creating')
      self.bootstrap_repo.create_git_ref(
        ref=f'refs/heads/{branch_name}',
        sha=self.bootstrap_repo.get_branch('main').commit.sha,
      )

    request_json_file = f'{branch_name}.json'

    # Populate the json file only with useful stuff
    json_fields = [
      'github_repo',
      'repo_description',
      'base_template',
      'jira_project_keys',
      'github_project_visibility',
      'product',
      'github_project_teams_write',
      'github_projects_teams_admin',
      'github_project_branch_protection_restricted_teams',
      'prod_alerts_severity_label',
      'nonprod_alerts_severity_label',
      'slack_channel_nonprod_release_notify',
      'slack_channel_prod_release_notify',
      'slack_channel_security_scans_notify',
      'requester_name',
      'requester_email',
      'requester_team',
    ]

    request_json = {key: request.get(key) for key in json_fields if key in request}

    create_file = False
    # Check if the project-request.json file exists and update it if it does
    try:
      json_file = self.bootstrap_repo.get_contents(
        f'requests/{request_json_file}', ref=branch_name
      )
      if json_file and not isinstance(json_file, list):
        self.bootstrap_repo.update_file(
          json_file.path,
          f'Updating {request_json_file} with details for {request.get("github_repo")}',
          json.dumps(request_json, indent=2),
          json_file.sha,
          branch=branch_name,
        )

    except GithubException as e:
      if e.status == 404:
        # Need to create the project.json file if it's not there
        create_file = True
      else:
        log.error(
          f'Failed to update requests/{request_json_file} in {self.bootstrap_repo.name} - {e.data} - please fix this this and re-run'
        )
        sys.exit(1)

    if create_file:
      try:
        log.debug(f'Creating file: {request_json_file}')
        self.bootstrap_repo.create_file(
          f'requests/{request_json_file}',
          f'Creating requests/{request_json_file} with details for {request.get("github_repo")}',
          json.dumps(request_json, indent=2),
          branch=branch_name,
        )
      except GithubException as e:
        log.error(
          f'Failed to create requests/{request_json_file} in {self.bootstrap_repo.name} - {e.data} - please fix this this and re-run'
        )
        sys.exit(1)

    github_pulls = self.bootstrap_repo.get_pulls(
      state='open',
      sort='created',
      base='main',
      head=f'{self.github_org}:{branch_name}',
    )

    log.debug(f'Current pulls for {branch_name}: {github_pulls.totalCount}')
    if github_pulls.totalCount == 0:
      # Create a new PR if one doesn't exist
      log.info(f'Creating PR for {branch_name}')
      pr = self.bootstrap_repo.create_pull(
        title=f'Project request for {request.get("github_repo")}',
        body=f'Project request raised for {request.get("github_repo")}',
        head=branch_name,
        base='main',
      )
      pr.enable_automerge('MERGE')
      request['request_github_pr_number'] = pr.number
      request['output_status'] = 'New'
      request['request_github_pr_status'] = 'Raised'
    else:
      log.info(f'PR already exists for {branch_name}')
      request['request_github_pr_number'] = github_pulls[0].number
      request['output_status'] = 'Updated'
      request['request_github_pr_status'] = 'Updated'

    return request

  def delete_old_workflows(self):
    try:
      if bootstrap_workflow := [
        workflow
        for workflow in self.bootstrap_repo.get_workflows()
        if workflow.name == 'Bootstrap - poll for repo requests'
      ]:
        workflow_runs = bootstrap_workflow[0].get_runs()
        run_qty = workflow_runs.totalCount
        if run_qty > 12:
          log.debug(
            f'Workflow {bootstrap_workflow[0].name} has {run_qty} runs - cropping to 12'
          )
          for run in workflow_runs[12:]:
            run.delete()

    except GithubException as e:
      log.warning(
        f'Encountered an issue removing old workflow runs in {self.bootstrap_repo.name} - {e.data} - please fix this this and re-run'
      )

  def get_repo(self, repo_name):
    try:
      self.repo = self.org.get_repo(f'{repo_name}')
    except GithubException as e:
      if e.status == 404:
        return False
      else:
        log.error(
          f'Failed to get Github repository information for {repo_name}: {e.data} - please correct this and re-run'
        )
        sys.exit(1)
    return True

  def create_repo(self, project_params):
    if project_params['github_template_repo']:
      # create repository from template
      # Headers for the request
      headers = {
        'Authorization': f'token {self.github_access_token}',
        'Accept': 'application/vnd.github.v3+json',
      }

      # Data for the request
      data = {
        'owner': project_params['github_org'],
        'name': project_params['github_repo'],
        'description': project_params['description'],
      }

      # Make the request to create a new repository from a template
      response = requests.post(
        f'https://api.github.com/repos/{project_params["github_org"]}/{project_params["github_template_repo"]}/generate',
        headers=headers,
        json=data,
      )

      if response.status_code == 201:
        log.info(f'Repository {project_params["github_repo"]} created successfully.')
      else:
        log.error(
          f'Failed to create repository: {response.status_code} - {response.text}'
        )
        sys.exit(1)

      # load the repo details into the repo object
      self.repo = self.session.get_repo(
        f'{project_params["github_org"]}/{project_params["github_repo"]}'
      )

    else:
      # create fresh new repository

      headers = {
        'Authorization': f'token {self.github_access_token}',
        'Accept': 'application/vnd.github.v3+json',
      }

      # Data for the request
      data = {
        'name': project_params['github_repo'],
        'description': project_params['description'],
      }

      # Make the request to create a new repository from a template
      response = requests.post(
        f'https://api.github.com/orgs/{project_params["github_org"]}/repos',
        headers=headers,
        json=data,
      )

      if response.status_code == 201:
        log.info(f'Repository {project_params["github_repo"]} created successfully.')
      else:
        log.error(
          f'Failed to create repository: {response.status_code} - {response.text}'
        )
        sys.exit(1)

      # and populate it with a basic README.md
      self.repo = self.session.get_repo(
        f'{project_params["github_org"]}/{project_params["github_repo"]}'
      )
      try:
        file_name = 'README.md'
        file_contents = (
          f'# {project_params["github_repo"]}\n{project_params["description"]}'
        )
        self.repo.create_file(file_name, 'commit', file_contents)
      except GithubException as e:
        log.error(
          f'Failed to create Github README.md - {e.data} - please correct this and re-run'
        )
        sys.exit(1)

    # poll for the repo to prevent race conditions
    repo_ready = False
    check_count = 0
    log.debug('Checking to see if the repo is ready yet..')
    while not repo_ready and check_count < 10:
      sleep(5)
      try:
        log.debug(f'Attempt: {check_count}')
        self.repo.edit(default_branch='main')
        repo_ready = True
      except Exception:
        check_count += 1
        continue

    if not repo_ready:
      log.error(
        f'Repository {project_params["github_repo"]} not ready after 10 attempts - please check and re-run'
      )
      sys.exit(1)

  def add_repo_to_runner_group(self, repo_name, runner_group_name):
    repo = self.org.get_repo(repo_name)
    if not repo:
      log.error(
        f'Could not find repo {repo_name} - not trying to add it to the runner group'
      )
      return False
    repo_id = repo.id

    headers = {
      'Authorization': f'token {self.github_access_token}',
      'Accept': 'application/vnd.github.v3+json',
    }
    try:
      r = requests.get(
        f'https://api.github.com/orgs/{self.github_org}/actions/runner-groups',
        headers=headers,
      )
      r.raise_for_status()
      groups = r.json().get('runner_groups', [])
      if runner_group := next(g for g in groups if g['name'] == runner_group_name):
        runner_group_id = runner_group['id']
      else:
        log.error(
          f'Runner group {runner_group_name} not found - not possible to add repository {repo_name} to runner group'
        )
        return False
    except GithubException as e:
      log.error(f'Unable to get a list of runner groups: {e}')
      return False

    try:
      r = requests.put(
        f'https://api.github.com/orgs/{self.github_org}/actions/runner-groups/{runner_group_id}/repositories/{repo_id}',
        headers=headers,
      )
      r.raise_for_status()
      log.info(
        f'Repo {repo_name} added to runner group {runner_group_name} (id: {runner_group_id}).'
      )
    except GithubException as e:
      log.error(
        f'Unable to add repository {repo_name} to runner group {runner_group_name}: {e}'
      )
      return False

    return True
