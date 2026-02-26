import React, { useState } from 'react'
import CreateTeam from './steps/CreateTeam'
import ConnectJira from './steps/ConnectJira'
import ChooseModel from './steps/ChooseModel'
import Complete from './steps/Complete'
import { getJwtClaims } from './api'

const STEPS = [
  { label: 'Create Team', component: CreateTeam },
  { label: 'Connect Jira', component: ConnectJira },
  { label: 'AI Model', component: ChooseModel },
  { label: 'Done', component: Complete },
]

export default function OnboardingWizard() {
  const [currentStep, setCurrentStep] = useState(0)
  const [wizardData, setWizardData] = useState(() => {
    const claims = getJwtClaims()
    return {
      tenantId: '',
      teamName: '',
      adminName: '',
      email: claims?.email || '',
      cognitoSub: claims?.sub || '',
      jira: null,
      aiModel: '',
    }
  })

  const progress = Math.round(((currentStep) / (STEPS.length - 1)) * 100)

  const handleNext = (stepData) => {
    setWizardData(prev => ({ ...prev, ...stepData }))
    setCurrentStep(prev => Math.min(prev + 1, STEPS.length - 1))
  }

  const handleBack = () => {
    setCurrentStep(prev => Math.max(prev - 1, 0))
  }

  const StepComponent = STEPS[currentStep].component

  return (
    <div className="container wizard-container">
      <div className="wizard-header">
        <h1>Welcome to T3nets</h1>
        <p>Let's set up your team in a few quick steps.</p>
      </div>

      <div className="wizard-progress">
        <div className="wizard-steps-indicator">
          {STEPS.map((step, i) => (
            <span
              key={step.label}
              className={
                i === currentStep ? 'active' : i < currentStep ? 'completed' : ''
              }
            >
              {step.label}
            </span>
          ))}
        </div>
        <div className="progress">
          <div
            className="progress-bar"
            role="progressbar"
            style={{ width: `${progress}%` }}
            aria-valuenow={progress}
            aria-valuemin="0"
            aria-valuemax="100"
          />
        </div>
      </div>

      <StepComponent
        data={wizardData}
        onNext={handleNext}
        onBack={handleBack}
        isFirst={currentStep === 0}
        isLast={currentStep === STEPS.length - 1}
      />
    </div>
  )
}
