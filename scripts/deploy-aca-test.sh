#!/bin/bash
set -e

script_dir="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"

if [[ -f "$script_dir/../.env" ]]; then
	echo "Loading .env"
	source "$script_dir/../.env"
fi


if [[ ! -f "$script_dir/../infra/output.json" ]]; then
  echo "output.json not found - have you deployed the base infra?"
  exit 1
fi

api_fqdn=$(cat $script_dir/../infra/output.json  | jq -r .apiSimFqdn)
if [[ -z "$api_fqdn" ]]; then
  echo "API endpoint (apiSimFqdn) not found in output.json"
  exit 1
fi

echo "=="
echo "== Testing API is up and running at https://$api_fqdn"
echo "=="

curl -s -w "\nGot response: %{http_code}" https://$api_fqdn/
echo -e "\n\n"
