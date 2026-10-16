import os
import sys
from io import StringIO
from github import Github, GithubException
from ruamel.yaml import YAML
from ruamel.yaml.scalarstring import DoubleQuotedScalarString

# 1. Target repositories
REPOSITORIES = [
    # Add here the repos to modify...
    #{"name": "pagopa/pagopa-anonymizer", "domain": "shared"}
]

BRANCH_NAME = "update-ingress-haproxy"
COMMIT_MESSAGE = "chore: update microservice-chart to 8.8.0 and configure haproxy block"
PR_TITLE = "chore: Migrate to microservice-chart 8.8.0 and configure HAProxy"
PR_BODY = """<!--- Please always add a PR description as if nobody knows anything about the context these changes come from. -->
<!--- Even if we are all from our internal team, we may not be on the same page. -->
<!--- Write this PR as you were contributing to a public OSS project, where nobody knows you and you have to earn their trust. -->
<!--- This will improve our projects in the long run! Thanks. -->

#### List of Changes

<!--- Describe your changes in detail -->
- Migrate ingress from nginx to HAProxy
- Update microservice-chart to 8.8.0

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


def get_yaml_parser():
    yaml = YAML()
    yaml.preserve_quotes = True
    yaml.indent(mapping=2, sequence=4, offset=2)
    return yaml


def process_chart_yaml(content_str):
    """
    Update microservice-chart version to 8.8.0 in Chart.yaml
    """
    yaml_parser = get_yaml_parser()
    data = yaml_parser.load(content_str)

    if not isinstance(data, dict) or "dependencies" not in data:
        return None, False

    modified = False
    for dep in data["dependencies"]:
        if isinstance(dep, dict) and dep.get("name") == "microservice-chart":
            if dep.get("version") != "8.8.1":
                dep["version"] = "8.8.1"
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
    """
    if not full_path:
        return "/"
    
    clean_path = str(full_path).strip()
    parts = clean_path.split("/")
    
    # Prende la prima parte dopo la radice e assicura lo slash finale
    if len(parts) >= 2 and parts[1]:
        return f"/{parts[1]}/"
    
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

        haproxy_config = {
            "create": True,
            "host": "haproxy."+domain+".internal.platform.pagopa.it",
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

def process_repository(gh, repo_name, domain, pr_elaborated):
    print(f"\n---> Modifing Repo: {repo_name}")
    try:
        repo = gh.get_repo(repo_name)
    except GithubException as e:
        print(f"ERROR: Impossible to access repo {repo_name}: {e}")
        return

    default_branch = repo.default_branch

    # 1. Check/Create the dedicated branch
    branch_exists = False
    try:
        repo.get_git_ref(f"heads/{BRANCH_NAME}")
        branch_exists = True
        print(f"The branch '{BRANCH_NAME}' already exists. It will be checked and possibly updated.")
    except GithubException:
        source_ref = repo.get_git_ref(f"heads/{default_branch}")
        repo.create_git_ref(ref=f"refs/heads/{BRANCH_NAME}", sha=source_ref.object.sha)
        print(f"Branch '{BRANCH_NAME}' created successfully.")

    # 2. Retrieve YAML files in the helm/ folder from the target branch (if it exists) or from the main branch
    target_ref = BRANCH_NAME if branch_exists else default_branch
    
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
        elif file_content.name.endswith((".yaml", ".yml")):
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
                target_file_info = repo.get_contents(file_info.path, ref=BRANCH_NAME)
            except GithubException:
                target_file_info = file_info
        else:
            target_file_info = file_info

        decoded_content = target_file_info.decoded_content.decode("utf-8")
        new_content = None
        is_modified = False

        if target_file_info.name.lower() == "chart.yaml":
            new_content, is_modified = process_chart_yaml(decoded_content)
        else:
            new_content, is_modified = process_values_yaml(decoded_content, domain)

        if is_modified:
            print(f"Update/Modification applied to: {target_file_info.path}")
            repo.update_file(
                path=target_file_info.path,
                message=f"{COMMIT_MESSAGE} on {target_file_info.name}",
                content=new_content,
                sha=target_file_info.sha,  # Insert the correct SHA from the target branch
                branch=BRANCH_NAME
            )
            updated_any = True
        else:
            print(f"File already updated / no changes for: {target_file_info.path}")

    # 4. Check or create the Pull Request
    try:
        prs = repo.get_pulls(state="open", head=f"{repo.owner.login}:{BRANCH_NAME}")
        if prs.totalCount > 0:
            pr = prs[0]
            if updated_any:
                print(f"SUCCESS: PR #{pr.number} updated with new commits -> {pr.html_url}")
            else:
                print(f"Info: PR #{pr.number} is already open and up-to-date -> {pr.html_url}")
        else:
            # Create the PR only if the branch has modifications or if for some reason it was missing
            pr = repo.create_pull(
                title=PR_TITLE,
                body=PR_BODY,
                head=BRANCH_NAME,
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
        process_repository(gh, repo_info["name"], repo_info["domain"], pr_elaborated)

    print(f"Total: {pr_elaborated['total']}")
    print(f"PR: {pr_elaborated['pr_links']}")


if __name__ == "__main__":
    main()