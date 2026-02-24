import React, { useState, useEffect } from 'react'
import { activateTenant, assignTenant } from '../api'

export default function Complete({ data }) {
  const [activating, setActivating] = useState(true)
  const [error, setError] = useState('')
  const [done, setDone] = useState(false)

  useEffect(() => {
    finalize()
  }, [])

  const finalize = async () => {
    try {
      const activateResult = await activateTenant(data.tenantId)
      if (activateResult.error) {
        setError(activateResult.error)
        setActivating(false)
        return
      }

      const assignResult = await assignTenant(data.tenantId)
      if (assignResult.error && !assignResult.ok) {
        console.warn('Tenant assignment skipped:', assignResult.error || assignResult.message)
      }

      setDone(true)
    } catch (err) {
      setError(err.message || 'Failed to finalize setup')
    }
    setActivating(false)
  }

  const handleGoToChat = () => {
    window.location.href = '/chat'
  }

  return (
    <div className="card wizard-card">
      <div className="card-header">Setup Complete</div>
      <div className="card-body">
        {error && <div className="alert alert-danger">{error}</div>}

        {activating ? (
          <div className="text-center py-4">
            <div className="spinner-border" role="status" />
            <p className="mt-3 text-secondary">Finalizing your setup...</p>
          </div>
        ) : done ? (
          <>
            <div className="text-center mb-4">
              <svg xmlns="http://www.w3.org/2000/svg" width="48" height="48" fill="currentColor" className="text-success" viewBox="0 0 16 16">
                <path d="M16 8A8 8 0 1 1 0 8a8 8 0 0 1 16 0m-3.97-3.03a.75.75 0 0 0-1.08.022L7.477 9.417 5.384 7.323a.75.75 0 0 0-1.06 1.06L6.97 11.03a.75.75 0 0 0 1.079-.02l3.992-4.99a.75.75 0 0 0-.01-1.05z"/>
              </svg>
              <h4 className="mt-3">You're all set!</h4>
              <p className="text-secondary">
                Your team <strong>{data.teamName}</strong> is ready to go.
              </p>
            </div>

            <div className="mb-4">
              <div className="summary-item">
                <span className="summary-label">Team</span>
                <span className="summary-value">{data.teamName}</span>
              </div>
              <div className="summary-item">
                <span className="summary-label">Team ID</span>
                <span className="summary-value">{data.tenantId}</span>
              </div>
              <div className="summary-item">
                <span className="summary-label">Admin</span>
                <span className="summary-value">{data.adminName} ({data.email || 'local'})</span>
              </div>
              <div className="summary-item">
                <span className="summary-label">Jira</span>
                <span className="summary-value">
                  {data.jira ? `Connected (${data.jira.projectKey})` : 'Not connected'}
                </span>
              </div>
              <div className="summary-item">
                <span className="summary-label">AI Model</span>
                <span className="summary-value">{data.aiModel || 'Default'}</span>
              </div>
            </div>

            <div className="text-center">
              <button className="btn btn-primary btn-lg" onClick={handleGoToChat}>
                Go to Chat
              </button>
            </div>
          </>
        ) : (
          <div className="text-center py-4">
            <div className="alert alert-warning">
              Setup encountered an issue. You can try again or go to chat.
            </div>
            <button className="btn btn-primary" onClick={handleGoToChat}>
              Go to Chat anyway
            </button>
          </div>
        )}
      </div>
    </div>
  )
}
