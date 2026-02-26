/**
 * API helper for onboarding wizard.
 * Wraps fetch with auth headers from localStorage (Cognito JWT).
 */

function getAuthHeaders() {
  const headers = { 'Content-Type': 'application/json' }
  const token = localStorage.getItem('t3nets_id_token')
  if (token) {
    headers['Authorization'] = `Bearer ${token}`
  }
  return headers
}

/**
 * Decode JWT payload to extract user claims (sub, email, custom:tenant_id).
 */
export function getJwtClaims() {
  const token = localStorage.getItem('t3nets_id_token')
  if (!token) return null
  try {
    const payload = token.split('.')[1]
    return JSON.parse(atob(payload.replace(/-/g, '+').replace(/_/g, '/')))
  } catch {
    return null
  }
}

export async function createTenant(data) {
  const res = await fetch('/api/admin/tenants', {
    method: 'POST',
    headers: getAuthHeaders(),
    body: JSON.stringify(data),
  })
  return res.json()
}

export async function updateTenant(tenantId, data) {
  const res = await fetch(`/api/admin/tenants/${tenantId}`, {
    method: 'PUT',
    headers: getAuthHeaders(),
    body: JSON.stringify(data),
  })
  return res.json()
}

export async function activateTenant(tenantId) {
  const res = await fetch(`/api/admin/tenants/${tenantId}/activate`, {
    method: 'PATCH',
    headers: getAuthHeaders(),
    body: JSON.stringify({}),
  })
  return res.json()
}

export async function storeIntegration(name, credentials) {
  const res = await fetch(`/api/integrations/${name}`, {
    method: 'POST',
    headers: getAuthHeaders(),
    body: JSON.stringify(credentials),
  })
  return res.json()
}

export async function testIntegration(name, credentials) {
  const res = await fetch(`/api/integrations/${name}/test`, {
    method: 'POST',
    headers: getAuthHeaders(),
    body: JSON.stringify(credentials),
  })
  return res.json()
}

export async function assignTenant(tenantId) {
  const res = await fetch('/api/auth/assign-tenant', {
    method: 'POST',
    headers: getAuthHeaders(),
    body: JSON.stringify({ tenant_id: tenantId }),
  })
  return res.json()
}

export async function getSettings() {
  const res = await fetch('/api/settings', {
    headers: getAuthHeaders(),
  })
  return res.json()
}

export async function getAuthConfig() {
  const res = await fetch('/api/auth/config')
  return res.json()
}
