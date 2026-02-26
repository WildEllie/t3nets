import React, { useState, useEffect } from 'react'
import { getSettings, updateTenant } from '../api'

const MODEL_HINTS = {
  'claude': 'Best quality, higher cost',
  'nova': 'Fast & affordable',
  'llama': 'Open-source, basic tasks',
}

function getModelHint(modelId) {
  for (const [key, hint] of Object.entries(MODEL_HINTS)) {
    if (modelId.toLowerCase().includes(key)) return hint
  }
  return ''
}

export default function ChooseModel({ data, onNext, onBack }) {
  const [models, setModels] = useState([])
  const [selectedModel, setSelectedModel] = useState(data.aiModel || '')
  const [defaultModel, setDefaultModel] = useState('')
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => {
    loadModels()
  }, [])

  const loadModels = async () => {
    try {
      const settings = await getSettings()
      setModels(settings.models || [])
      setDefaultModel(settings.ai_model || '')
      if (!selectedModel) setSelectedModel(settings.ai_model || '')
    } catch {
      setError('Failed to load available models')
    }
    setLoading(false)
  }

  const handleSave = async () => {
    setSaving(true)
    setError('')
    try {
      if (selectedModel && selectedModel !== defaultModel) {
        const result = await updateTenant(data.tenantId, { ai_model: selectedModel })
        if (result.error) {
          setError(result.error)
          setSaving(false)
          return
        }
      }
      onNext({ aiModel: selectedModel })
    } catch (err) {
      setError(err.message || 'Failed to save model selection')
      setSaving(false)
    }
  }

  const handleSkip = () => onNext({ aiModel: defaultModel })

  if (loading) {
    return (
      <div className="card wizard-card">
        <div className="card-header">Step 3: Choose AI Model</div>
        <div className="card-body text-center py-5">
          <div className="spinner-border" role="status" />
          <p className="mt-3 text-secondary">Loading available models...</p>
        </div>
      </div>
    )
  }

  return (
    <div className="card wizard-card">
      <div className="card-header">Step 3: Choose AI Model</div>
      <div className="card-body">
        {error && (
          <div className="alert alert-danger alert-dismissible" role="alert">
            {error}
            <button type="button" className="btn-close" onClick={() => setError('')} />
          </div>
        )}

        <p className="text-secondary mb-3">
          Choose the AI model your team will use. You can change this later in Settings.
        </p>

        <div className="row g-3 mb-3">
          {models.map((model) => (
            <div className="col-12 col-sm-6" key={model.id}>
              <div
                className={`card model-card ${selectedModel === model.id ? 'selected' : ''}`}
                onClick={() => setSelectedModel(model.id)}
                role="button"
              >
                <div className="card-body">
                  <div className="model-name">
                    {model.display_name || model.short_name || model.id}
                    {model.id === defaultModel && (
                      <span className="badge bg-primary ms-2">Default</span>
                    )}
                  </div>
                  <div className="model-desc">
                    {getModelHint(model.id) || model.description || ''}
                  </div>
                </div>
              </div>
            </div>
          ))}
        </div>

        {models.length === 0 && (
          <div className="alert alert-info">
            No models available. The default model will be used.
          </div>
        )}

        <div className="wizard-nav">
          <button type="button" className="btn btn-link text-secondary" onClick={onBack}>
            Back
          </button>
          <div>
            <button type="button" className="btn btn-link me-2" onClick={handleSkip}>
              Use default
            </button>
            <button className="btn btn-primary" onClick={handleSave} disabled={saving}>
              {saving ? (
                <>
                  <span className="spinner-border spinner-border-sm me-2" role="status" />
                  Saving...
                </>
              ) : (
                'Continue'
              )}
            </button>
          </div>
        </div>
      </div>
    </div>
  )
}
