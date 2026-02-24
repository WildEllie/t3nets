import React from 'react'
import './styles.scss'
import OnboardingWizard from './OnboardingWizard'

export default function App() {
  return (
    <div className="min-vh-100 d-flex align-items-center" data-bs-theme="dark">
      <OnboardingWizard />
    </div>
  )
}
