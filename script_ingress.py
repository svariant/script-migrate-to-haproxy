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
    {"name": "pagopa/pagopa-anonymizer", "domain": "shared"}
]

CHART_VERSION = "8.8.2"

TASK="PIDM-2502" 

PR_BODY_HEADER = """<!--- Please always add a PR description as if nobody knows anything about the context these changes come from. -->
<!--- Even if we are all from our internal team, we may not be on the same page. -->
<!--- Write this PR as you were contributing to a public OSS project, where nobody knows you and you have to earn their trust. -->
<!--- This will improve our projects in the long run! Thanks. -->

#### List of Changes

<!--- Describe your changes in detail -->
"""

PR_BODY_FOOTER = f"""
#### Motivation and Context

<!--- Why is this change required? What problem does it solve? -->
[{TASK}](https://pagopa.atlassian.net/browse/{TASK})
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
BRANCH_NAME_1 = f"{TASK}-update-microservice-chart-8-8-2"
COMMIT_MESSAGE_1 = f"chore: update microservice-chart to {CHART_VERSION}"
PR_TITLE_1 = f"[{TASK}] chore: Update microservice-chart to {CHART_VERSION}"
PR_BODY_1 = PR_BODY_HEADER + f"- Update microservice-chart to {CHART_VERSION}\n" + PR_BODY_FOOTER

# --- PR 2: solo blocco haproxy ---
BRANCH_NAME_2 = f"{TASK}-update-ingress-haproxy"
COMMIT_MESSAGE_2 = "chore: configure haproxy block"
PR_TITLE_2 = f"[{TASK}] chore: Configure HAProxy ingress"
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


def extract_haproxy_path(full_path):
    if not full_path:
        return "/", "/"

    full_path = str(full_path).strip()

    # Rimuove il pattern regex Nginx finale "(.*)"
    # dal path utilizzato da HAProxy.
    if full_path.endswith("(.*)"):
        haproxy_path = full_path[:-4]

        if not haproxy_path.endswith("/"):
            haproxy_path += "/"

        return haproxy_path, "/"

    return full_path, "/"


def process_values_yaml(content_str, domain=None):
    """
    Aggiunge HAProxy solamente agli ingress che dichiarano
    esplicitamente un path.

    Non modifica:
      - canaryDelivery.ingress
      - ingress base usato come anchor (&ingress)
      - ingress senza path
      - ingress che hanno già haproxy
    """

    lines = content_str.splitlines(keepends=True)

    blocks = []

    # -------------------------------------------------------
    # 1. Individua tutti i blocchi "ingress:"
    # -------------------------------------------------------
    for i, line in enumerate(lines):

        match = re.match(
            r"^(\s*)ingress:\s*(?:&[A-Za-z0-9_-]+)?\s*(?:#.*)?(?:\r?\n)?$",
            line,
        )

        if not match:
            continue

        indent = len(match.group(1))

        # Trova la fine del blocco ingress
        end = len(lines)

        for j in range(i + 1, len(lines)):

            current = lines[j]

            if not current.strip():
                continue

            if current.lstrip().startswith("#"):
                continue

            current_indent = len(current) - len(current.lstrip(" "))

            if current_indent <= indent:
                end = j
                break

        blocks.append(
            {
                "start": i,
                "end": end,
                "indent": indent,
            }
        )

    if not blocks:
        print("WARN: nessun ingress trovato")
        return None, False

    # -------------------------------------------------------
    # 2. Recupera host comune dall'anchor &ingress
    # -------------------------------------------------------

    common_host = None

    for block in blocks:

        ingress_line = lines[block["start"]]

        # Ci interessa in particolare:
        #
        # ingress: &ingress
        #
        if "&ingress" not in ingress_line:
            continue

        body = "".join(
            lines[
                block["start"] + 1:
                block["end"]
            ]
        )

        host_match = re.search(
            r'(?m)^\s+host:\s*["\']?([^"\'\r\n#]+)["\']?\s*(?:#.*)?$',
            body,
        )

        if host_match:
            common_host = host_match.group(1).strip()
            break

    # -------------------------------------------------------
    # 3. Aggiunge HAProxy solamente agli ingress con path
    # -------------------------------------------------------

    insertions = []

    for block in blocks:

        body = "".join(
            lines[
                block["start"] + 1:
                block["end"]
            ]
        )

        # Già configurato
        if re.search(
            r"(?m)^\s+haproxy:\s*(?:#.*)?$",
            body,
        ):
            print(
                f"SKIP: ingress riga "
                f"{block['start'] + 1}: HAProxy già presente"
            )
            continue

        # IMPORTANTE:
        # deve esserci path esplicito nel blocco corrente.
        path_match = re.search(
            r'(?m)^\s+path:\s*["\']?([^"\'\r\n#]+)["\']?\s*(?:#.*)?$',
            body,
        )

        if not path_match:
            print(
                f"SKIP: ingress riga "
                f"{block['start'] + 1}: nessun path esplicito"
            )
            continue

        path = path_match.group(1).strip()

        # ---------------------------------------------------
        # Host
        # ---------------------------------------------------

        host_match = re.search(
            r'(?m)^\s+host:\s*["\']?([^"\'\r\n#]+)["\']?\s*(?:#.*)?$',
            body,
        )

        if host_match:
            host = host_match.group(1).strip()
        else:
            # PostgreSQL usa:
            #
            # !!merge <<: *ingress
            #
            # quindi eredita l'host dall'anchor.
            host = common_host

        if not host:
            print(
                f"WARN: ingress riga "
                f"{block['start'] + 1}: host non trovato"
            )
            continue

        # ---------------------------------------------------
        # Conversione path
        # ---------------------------------------------------

        haproxy_path, rewrite_target = extract_haproxy_path(path)

        child_indent = " " * (block["indent"] + 2)
        grandchild_indent = " " * (block["indent"] + 4)

        block_text = (
            f"{child_indent}haproxy:\n"
            f"{grandchild_indent}create: true\n"
            f'{grandchild_indent}host: "{host}"\n'
            f'{grandchild_indent}path: "{haproxy_path}"\n'
            f'{grandchild_indent}rewriteTarget: "{rewrite_target}"\n'
            f"{grandchild_indent}annotations:\n"
            f'{grandchild_indent}  haproxy.org/check: "true"\n'
        )

        insertions.append(
            (block["end"], block_text)
        )

    if not insertions:
        return None, False

    # -------------------------------------------------------
    # 4. Inserisci dal basso verso l'alto
    # -------------------------------------------------------

    for index, text in sorted(
        insertions,
        key=lambda x: x[0],
        reverse=True,
    ):
        lines.insert(index, text)

    return "".join(lines), True
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
        "match_file": lambda name: (
            name.lower().startswith("values")
            and name.lower().endswith((".yaml", ".yml"))
        ),
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
