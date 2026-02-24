import React, { useState } from 'react'
import { testIntegration, storeIntegration } from '../api'

export default function ConnectJira({ data, onNext, onBack }) {
  const [url, setUrl] = useState('')
  const [email, setEmail] = useState('')
  const [apiToken, setApiToken] = useState('')
  const [projectKey, setProjectKey] = useState('')
  const [boardId, setBoardId] = useState('')
  const [testing, setTesting] = useState(false)
  const [saving, setSaving] = useState(false)
  const [testResult, setTestResult] = useState(null)
  const [error, setError] = useState('')

  const handleTest = async () => {
    setTesting(true)
    setTestResult(null)
    setError('')
    try {
      const result = await testIntegration('jira', { url, email, api_token: apiToken })
      setTestResult(result)
    } catch (err) {
      setTestResult({ ok: false, error: err.message })
    }
    setTesting(false)
  }

  const handleSave = async () => {
    setSaving(true)
    setError('')
    try {
      const creds = {
        url: url.replace(/\/+$/, ''),
        email,
        api_token: apiToken,
        project_key: projectKey,
      }
      if (boardId) creds.board_id = boardId

      const result = await storeIntegration('jira', creds)
      if (result.error) {
        setError(result.error)
        setSaving(false)
        return
      }
      onNext({ jira: { url, email, projectKey, boardId, connected: true } })
    } catch (err) {
      setError(err.message || 'Failed to save Jira credentials')
      setSaving(false)
    }
  }

  const handleSkip = () => onNext({ jira: null })

  const canTest = url && email && apiToken
  const canSave = testResult?.ok && projectKey

  return (
    <div className="card wizard-card">
      <div className="card-header">Step 2: Connect Jira</div>
      <div className="card-body">
        {error && (
          <div className="alert alert-danger alert-dismissible" role="alert">
            {error}
            <button type="button" className="btn-close" onClick={() => setError('')} />
          </div>
        )}

        <p className="text-secondary mb-3">
          Connect your Jira instance to enable sprint status, release notes, and other project skills.
        </p>

        <div className="mb-3">
          <label htmlFor="jiraUrl" className="form-label">Jira URL</label>
          <input
            id="jiraUrl"
            className="form-control"
            placeholder="https://your-team.atlassian.net"
            value={url}
            onChange={(e) => setUrl(e.target.value)}
          />
        </div>

        <div className="mb-3">
          <label htmlFor="jiraEmail" className="form-label">Jira Email</label>
          <input
            id="jiraEmail"
            type="email"
            className="form-control"
            placeholder="you@company.com"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
          />
        </div>

        <div className="mb-3">
          <label htmlFor="jiraToken" className="form-label">API Token</label>
          <input
            id="jiraToken"
            type="password"
            className="form-control"
            placeholder="Your Jira API token"
            value={apiToken}
            onChange={(e) => setApiToken(e.target.value)}
          />
          <div className="form-text">
            Generate one at{' '}
            <a href="https://id.atlassian.com/manage-profile/security/api-tokens" target="_blank" rel="noreferrer">
              Atlassian API Tokens
            </a>
          </div>
        </div>

        <div className="mb-3">
          <button
            type="button"
            className="btn btn-outline-secondary"
            onClick={handleTest}
            disabled={!canTest || testing}
          >
            {testing ? (
              <>
                <span className="spinner-border spinner-border-sm me-2" role="status" />
                Testing...
              </>
            ) : (
              'Test Connection'
            )}
          </button>

          {testResult && (
            <div className="connection-status">
              {testResult.ok ? (
                <div className="alert alert-success mt-2 mb-0 py-2">
                  Connected as {testResult.display_name || testResult.user}
                </div>
              ) : (
                <div className="alert alert-danger mt-2 mb-0 py-2">
                  {testResult.error || 'Connection failed'}
                </div>
              )}
            </div>
          )}
        </div>

        {testResult?.ok && (
          <>
            <div className="mb-3">
              <label htmlFor="projectKey" className="form-label">Project Key</label>
              <input
                id="projectKey"
                className="form-control"
                placeholder='e.g. "NV" or "PROJ"'
                value={projectKey}
                onChange={(e) => setProjectKey(e.target.value.toUpperCase())}
              />
              <div className="form-text">The short prefix on your Jira issues (e.g., NV-123).</div>
            </div>

            <div className="mb-3">
              <label htmlFor="boardId" className="form-label">Board ID (optional)</label>
              <input
                id="boardId"
                className="form-control"
                placeholder="e.g. 42"
                value={boardId}
                onChange={(e) => setBoardId(e.target.value)}
              />
              <div className="form-text">Found in your board URL: /board/42</div>
            </div>
          </>
        )}

        <div className="wizard-nav">
          <button type="button" className="btn btn-link text-secondary" onClick={onBack}>
            Back
          </button>
          <div>
            <button type="button" className="btn btn-link me-2" onClick={handleSkip}>
              Skip for now
            </button>
            <button
              type="button"
              className="btn btn-primary"
              onClick={handleSave}
              disabled={!canSave || saving}
            >
              {saving ? (
                <>
                  <span className="spinner-border spinner-border-sm me-2" role="status" />
                  Saving...
                </>
              ) : (
                'Save & Continue'
              )}
            </button>
          </div>
        </div>
      </div>
    </div>
  )
}
