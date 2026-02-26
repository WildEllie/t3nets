#!/bin/bash
set -e

API_ID="i9yxlqqro8"

terraform import -var-file=environments/dev.tfvars "module.api.aws_apigatewayv2_route.public_root" "${API_ID}/7i3hz0c"
terraform import -var-file=environments/dev.tfvars "module.api.aws_apigatewayv2_route.public_chat" "${API_ID}/ymmdg4a"
terraform import -var-file=environments/dev.tfvars "module.api.aws_apigatewayv2_route.public_settings_page" "${API_ID}/4yqklx0"
terraform import -var-file=environments/dev.tfvars "module.api.aws_apigatewayv2_route.public_auth_config" "${API_ID}/dpm1xwe"

echo "All 4 routes imported successfully"
