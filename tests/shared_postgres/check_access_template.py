"""Check compiled ARM scope expressions from stdin. No Azure API calls."""

import json
import re
import sys


def check(template):
    variables = template["variables"]
    for project, variable in (("eventharbor", "eventVnetId"),
                              ("pulseexchange", "pulseVnetId")):
        expected = ("[resourceId(subscription().subscriptionId,'rg-" + project
                    + "-prod','Microsoft.Network/virtualNetworks','vnet-" + project + "-prod')]")
        actual = re.sub(r"\s+", "", variables[variable])
        if actual != expected:
            raise ValueError("Cross-group VNet IDs must explicitly provide subscription ID and resource group.")
    # A subscription deployment interprets an ambiguous first string as a
    # subscription identifier. Compilation alone does not catch this error.
    if re.search(r"resourceId\(\s*'rg-", json.dumps(template)):
        raise ValueError("An ambiguous subscription-scope resourceId expression remains.")
    modules = {resource["name"]: resource for resource in template["resources"]
               if resource["type"] == "Microsoft.Resources/deployments"}
    for name, group in (("pg-copy-eh-peering", "rg-eventharbor-prod"),
                        ("pg-copy-px-peering", "rg-pulseexchange-prod"),
                        ("pg-copy-dns", "rg-eventharbor-prod")):
        if modules[name]["resourceGroup"] != group:
            raise ValueError("An existing-network module has the wrong deployment scope.")


if __name__ == "__main__":
    check(json.load(sys.stdin))
    print("SHARED_POSTGRES_COMPILED_SCOPE_CHECK_PASS")
