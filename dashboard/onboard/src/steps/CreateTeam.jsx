import React, { useState, useEffect } from 'react'
import { createTenant } from '../api'

function slugify(text) {
  return text
    .toLowerCase()
    .replace(/[^a-z0-9\s-]/g, '')
    .replace(/\s+/g, '-')
    .replace(/-+/g, '-')
    .replace(/^-|-$/g, '')
}

export default function CreateTeam({ data, onNext }) {
  const [teamName, setTeamName] = useState(data.teamName || '')
  const [tenantId, setTenantId] = useState(data.tenantId || '')
  const [adminName, setAdminName] = useState(data.adminName || '')
  const [idEdited, setIdEdited] = useState(false)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [validated, setValidated] = useState(false)

  useEffect(() => {
    if (!idEdited && teamName) {
      setTenantId(slugify(teamName))
    }
  }, [teamName, idEdited])

  const handleSubmit = async (e) => {
    e.preventDefault()
    setValidated(true)

    if (!teamName.trim() || !tenantId.trim() || !adminName.trim()) return
    if (tenantId.length < 3) return

    setLoading(true)
    setError('')

    try {
      const result = await createTenant({
        tenant_id: tenantId,
        name: teamName,
        status: 'onboarding',
        admin_user: {
          display_name: adminName,
          email: data.email,
          cognito_sub: data.cognitoSub,
        },
      })

      if (result.error) {
        setError(result.error)
        setLoading(false)
        return
      }

      onNext({ teamName, tenantId, adminName })
    } catch (err) {
      setError(err.message || 'Failed to create team')
      setLoading(false)
    }
  }

  return (
    <div className="card wizard-card">
      <div className="card-header">Step 1: Create Your Team</div>
      <div className="card-body">
        <form noValidate className={validated ? 'was-validated' : ''} onSubmit={handleSubmit}>
          {error && (
            <div className="alert alert-danger alert-dismissible" role="alert">
              {error}
              <button type="button" className="btn-close" onClick={() => setError('')} />
            </div>
          )}

          <div className="mb-3">
            <label htmlFor="teamName" className="form-label">Team Name</label>
            <input
              id="teamName"
              className="form-control"
              placeholder='e.g. "Acme Engineering"'
              value={teamName}
              onChange={(e) => setTeamName(e.target.value)}
              required
              autoFocus
            />
            <div className="invalid-feedback">Team name is required.</div>
          </div>

          <div className="mb-3">
            <label htmlFor="tenantId" className="form-label">Team ID</label>
            <input
              id="tenantId"
              className="form-control"
              value={tenantId}
              onChange={(e) => { setTenantId(e.target.value); setIdEdited(true) }}
              required
              minLength={3}
              pattern="[a-z0-9][a-z0-9-]*[a-z0-9]"
            />
            <div className="form-text">URL-safe identifier. Lowercase letters, numbers, and hyphens only.</div>
            <div className="invalid-feedback">Must be 3+ characters, lowercase letters/numbers/hyphens only.</div>
          </div>

          <div className="mb-3">
            <label htmlFor="adminName" className="form-label">Your Name</label>
            <input
              id="adminName"
              className="form-control"
              placeholder="Your display name"
              value={adminName}
              onChange={(e) => setAdminName(e.target.value)}
              required
            />
            <div className="invalid-feedback">Your name is required.</div>
          </div>

          {data.email && (
            <div className="mb-3">
              <label className="form-label">Email</label>
              <input className="form-control" value={data.email} disabled readOnly />
              <div className="form-text">From your login. You'll be the team admin.</div>
            </div>
          )}

          <div className="wizard-nav">
            <div />
            <button type="submit" className="btn btn-primary" disabled={loading}>
              {loading ? (
                <>
                  <span className="spinner-border spinner-border-sm me-2" role="status" />
                  Creating...
                </>
              ) : (
                'Create Team & Continue'
              )}
            </button>
          </div>
        </form>
      </div>
    </div>
  )
}
