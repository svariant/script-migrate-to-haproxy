import os
import sys
import re
from io import StringIO
from github import Github, GithubException
from ruamel.yaml import YAML
from ruamel.yaml.scalarstring import DoubleQuotedScalarString


# 1. Target repositories
REPOSITORIES = [
    # Add here the repos to modify...
    #{"name": "pagopa/pagopa-anonymizer", "domain": "shared"}
]

CHART_VERSION = "8.8.2"

PR_BODY_HEADER = """<!--- Please always add a PR description as if nobody knows anything about the context these changes come from. -->
<!--- Even if we are all from our internal team, we may not be on the same page. -->
<!--- Write this PR as you were contributing to a public OSS project, where nobody knows you and you have to earn their trust. -->
<!--- This will improve our projects in the long run! Thanks. -->

#### List of Changes

<!--- Describe your changes in detail -->
"""

PR_BODY_FOOTER = """
#### Motivation and Context

<!--- Why is this change required? What problem does it solve? -->
[PIDM-2501](https://pagopa.atlassian.net/browse/PIDM-2501)
Nginx will be EOL so its needed to migrate to HAProxy

#### How Has This Been Tested?

<!--- Please describe in detail how you tested your changes. -->
<!--- Include details of your testing environment, tests ran to see how -->
<!--- your change affects other areas of the code, etc. -->

#### Screenshots (if appropriate):

#### Types of changes

<!--- What types of changes does your code introduce? Put an `x` in all the boxes that apply: -->

- [ ] Bug fix (non-breaking change which fixes an issue)
- [ ] New feature (non-breaking change which adds functionality)
- [X] Breaking change (fix or feature that would cause existing functionality to not work as expected)

#### Checklist:

<!--- Go over all the following points, and put an `x` in all the boxes that apply. -->
<!--- If you're unsure about any of these, don't hesitate to ask. We're here to help! -->

- [ ] My change requires a change to the documentation.
- [ ] I have updated the documentation accordingly.
"""

# --- PR 1: solo upgrade microservice-chart ---
BRANCH_NAME_1 = "update-microservice-chart-8-8-2"
COMMIT_MESSAGE_1 = f"chore: update microservice-chart to {CHART_VERSION}"
PR_TITLE_1 = f"chore: Update microservice-chart to {CHART_VERSION}"
PR_BODY_1 = PR_BODY_HEADER + f"- Update microservice-chart to {CHART_VERSION}\n" + PR_BODY_FOOTER

# --- PR 2: solo blocco haproxy ---
BRANCH_NAME_2 = "update-ingress-haproxy"
COMMIT_MESSAGE_2 = "chore: configure haproxy block"
PR_TITLE_2 = "chore: Configure HAProxy ingress"
PR_BODY_2 = PR_BODY_HEADER + "- Migrate ingress from nginx to HAProxy (add haproxy block)\n" + PR_BODY_FOOTER




def get_yaml_parser():
    yaml = YAML()
    yaml.preserve_quotes = True
    yaml.indent(mapping=2, sequence=4, offset=2)
    return yaml


def process_chart_yaml(content_str, domain=None):
    """
    Update microservice-chart version to 8.8.2 in Chart.yaml
    """
    yaml_parser = get_yaml_parser()
    data = yaml_parser.load(content_str)

    if not isinstance(data, dict) or "dependencies" not in data:
        return None, False

    modified = False
    for dep in data["dependencies"]:
        if isinstance(dep, dict) and dep.get("name") == "microservice-chart":
            if dep.get("version") != CHART_VERSION:
                dep["version"] = CHART_VERSION
                modified = True

    if modified:
        stream = StringIO()
        yaml_parser.dump(data, stream)
        return stream.getvalue(), True

    return None, False


def extract_base_path(full_path):
    """
    Extract path from ingress and clean it
    Es: '/pagopa-anonymizer/(.*)' -> '/pagopa-anonymizer/'
        '/pagopa-api-config-core-service/p/(.*)' -> '/pagopa-api-config-core-service/p/'
    """
    if not full_path:
        return "/"
    
    clean_path = re.sub(r"\((\.\*|\.\+)\)\s*$", "", str(full_path).strip())
    return clean_path if clean_path.endswith("/") else f"{clean_path}/"


def process_values_yaml(content_str, domain):
    """
    Add haproxy block under ingress
    """
    yaml_parser = get_yaml_parser()
    data = yaml_parser.load(content_str)

    if not isinstance(data, dict):
        return None, False

    # Identity primary key (es. microservice-chart) or use root dictionary
    root_key = next(iter(data)) if len(data) == 1 else None
    chart_data = data[root_key] if root_key and isinstance(data[root_key], dict) else data

    modified = False

    # 1. Update Ingress / HAProxy
    if "ingress" in chart_data and isinstance(chart_data["ingress"], dict):
        ingress_dict = chart_data["ingress"]
        ingress_path = ingress_dict.get("path", "")
        base_path = extract_base_path(ingress_path)

        haproxy_host = ingress_dict.get("host")
        if not haproxy_host:
            print("WARN: nessun host trovato nell'ingress, blocco haproxy non aggiunto")
            return None, False

        haproxy_config = {
            "create": True,
            "host": haproxy_host,
            "path": base_path,
            "rewriteTarget": DoubleQuotedScalarString(r"/\1")
        }

        if ingress_dict.get("haproxy") != haproxy_config:
            ingress_dict["haproxy"] = haproxy_config
            modified = True

    # 2. Fix KEDA Autoscaling: Move 'type' to 'metricType' in metadata
    autoscaling = chart_data.get("autoscaling", {})
    if isinstance(autoscaling, dict) and autoscaling.get("enable"):
        triggers = autoscaling.get("triggers", [])
        if isinstance(triggers, list):
            for trigger in triggers:
                if isinstance(trigger, dict) and "metadata" in trigger:
                    metadata = trigger["metadata"]
                    if isinstance(metadata, dict) and "type" in metadata:
                        trigger["metricType"] = metadata.pop("type")
                        modified = True
                    else:
                        trigger["metricType"] = "Utilization"
                        modified = True

    if not modified:
        return None, False

    stream = StringIO()
    yaml_parser.dump(data, stream)
    return stream.getvalue(), True

JOBS = [
    {
        "name": "chart",
        "branch": BRANCH_NAME_1,
        "commit_message": COMMIT_MESSAGE_1,
        "pr_title": PR_TITLE_1,
        "pr_body": PR_BODY_1,
        "match_file": lambda name: name.lower() == "chart.yaml",
        "processor": process_chart_yaml,
    },
    {
        "name": "haproxy",
        "branch": BRANCH_NAME_2,
        "commit_message": COMMIT_MESSAGE_2,
        "pr_title": PR_TITLE_2,
        "pr_body": PR_BODY_2,
        "match_file": lambda name: name.lower() != "chart.yaml",
        "processor": process_values_yaml,
    },
]

def process_repository(gh, repo_name, domain, job, pr_elaborated):
    branch_name = job["branch"]
    print(f"\n---> [{job['name']}] Modifing Repo: {repo_name} - ")
    try:
        repo = gh.get_repo(repo_name)
    except GithubException as e:
        print(f"ERROR: Impossible to access repo {repo_name}: {e}")
        return

    default_branch = repo.default_branch

    # 1. Check/Create the dedicated branch
    branch_exists = False
    try:
        repo.get_git_ref(f"heads/{branch_name}")
        branch_exists = True
        print(f"The branch '{branch_name}' already exists. It will be checked and possibly updated.")
    except GithubException:
        source_ref = repo.get_git_ref(f"heads/{default_branch}")
        repo.create_git_ref(ref=f"refs/heads/{branch_name}", sha=source_ref.object.sha)
        print(f"Branch '{branch_name}' created successfully.")

    # 2. Retrieve YAML files in the helm/ folder from the target branch (if it exists) or from the main branch
    target_ref = branch_name if branch_exists else default_branch
    
    try:
        helm_contents = repo.get_contents("helm", ref=target_ref)
    except GithubException:
        print(f"WARN: Folder 'helm/' not found in {repo_name}. Skipping.")
        return

    files_to_process = []
    while helm_contents:
        file_content = helm_contents.pop(0)
        if file_content.type == "dir":
            helm_contents.extend(repo.get_contents(file_content.path, ref=target_ref))
        elif file_content.name.endswith((".yaml", ".yml")) and job["match_file"](file_content.name):
            files_to_process.append(file_content)

    if not files_to_process:
        print("No YAML files found.")
        return

    updated_any = False

    # 3. Process each file always retrieving the correct SHA from the target branch
    for file_info in files_to_process:
        # If the branch already existed, make sure to read the file and the most recent SHA from the target branch
        if branch_exists:
            try:
                target_file_info = repo.get_contents(file_info.path, ref=branch_name)
            except GithubException:
                target_file_info = file_info
        else:
            target_file_info = file_info

        decoded_content = target_file_info.decoded_content.decode("utf-8")
        new_content, is_modified = job["processor"](decoded_content, domain)

        if is_modified:
            print(f"Update/Modification applied to: {target_file_info.path}")
            repo.update_file(
                path=target_file_info.path,
                message=f"{job['commit_message']} on {target_file_info.name}",
                content=new_content,
                sha=target_file_info.sha,  # Insert the correct SHA from the target branch
                branch=branch_name
            )
            updated_any = True
        else:
            print(f"File already updated / no changes for: {target_file_info.path}")

    # 4. Check or create the Pull Request
    try:
        prs = repo.get_pulls(state="open", head=f"{repo.owner.login}:{branch_name}")
        if prs.totalCount > 0:
            pr = prs[0]
            if updated_any:
                print(f"SUCCESS: PR #{pr.number} updated with new commits -> {pr.html_url}")
            else:
                print(f"Info: PR #{pr.number} is already open and up-to-date -> {pr.html_url}")
        else:
            # Create the PR only if the branch has modifications or if for some reason it was missing
            pr = repo.create_pull(
                title=job["pr_title"],
                body=job["pr_body"],
                head=branch_name,
                base=default_branch
            )
            print(f"SUCCESS: New Pull Request created -> {pr.html_url}")
        pr_elaborated["total"] += 1
        pr_elaborated["pr_links"].append(pr.html_url)
    except GithubException as e:
        print(f"Info/Error managing PR: {e}")


def main():
    token = os.getenv("GITHUB_TOKEN")
    if not token:
        print("ERROR: Env GITHUB_TOKEN is not configured.")
        sys.exit(1)

    gh = Github(token)

    pr_elaborated = {"total": 0, "pr_links": []}
    for repo_info in REPOSITORIES:
        for job in JOBS:
            process_repository(gh, repo_info["name"], repo_info["domain"], job, pr_elaborated)

    print(f"Total: {pr_elaborated['total']}")
    print(f"PR: {pr_elaborated['pr_links']}")


if __name__ == "__main__":
    main()