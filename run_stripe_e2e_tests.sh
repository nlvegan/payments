#!/bin/bash

##############################################################################
# Stripe E2E Test Runner
#
# Usage:
#   ./run_stripe_e2e_tests.sh [OPTIONS]
#
# Options:
#   --headed     Run with visible browser (for debugging)
#   --debug      Run in Playwright debug mode
#   --ci         CI mode (headless, retry on failure)
#   --help       Show this help message
#
# Environment Variables (required):
#   STRIPE_TEST_SECRET_KEY        Stripe test secret key (sk_test_...)
#   STRIPE_TEST_PUBLISHABLE_KEY   Stripe test publishable key (pk_test_...)
#
# Optional Environment Variables:
#   STRIPE_WEBHOOK_SECRET   Webhook signing secret
#   TEST_SITE_URL          Base URL (default: http://test_site:8000)
#   ADMIN_PASSWORD         Admin password (default: admin)
#   NO_CLEANUP             Set to 1 to skip teardown cleanup
#
##############################################################################

set -e

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

# Script directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

log_info() { echo -e "${BLUE}[INFO]${NC} $1"; }
log_success() { echo -e "${GREEN}[SUCCESS]${NC} $1"; }
log_warning() { echo -e "${YELLOW}[WARNING]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }

show_help() {
    head -30 "$0" | tail -28 | sed 's/^# //' | sed 's/^#//'
    exit 0
}

# Default options
HEADED=""
DEBUG=""
CI_MODE=""

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --headed)
            HEADED="--headed"
            shift
            ;;
        --debug)
            DEBUG="--debug"
            shift
            ;;
        --ci)
            CI_MODE="1"
            export CI=1
            shift
            ;;
        --help|-h)
            show_help
            ;;
        *)
            log_error "Unknown option: $1"
            show_help
            ;;
    esac
done

# Check environment
log_info "Checking environment..."

if [[ -z "$STRIPE_TEST_PUBLISHABLE_KEY" ]]; then
    log_warning "STRIPE_TEST_PUBLISHABLE_KEY not set - tests will be skipped"
fi

if [[ -z "$STRIPE_TEST_SECRET_KEY" ]]; then
    log_warning "STRIPE_TEST_SECRET_KEY not set - tests will be skipped"
fi

# Check for Node.js
if ! command -v node &> /dev/null; then
    log_error "Node.js not found. Please install Node.js 18+"
    exit 1
fi

# Check for npm/npx
if ! command -v npx &> /dev/null; then
    log_error "npx not found. Please install npm"
    exit 1
fi

# Install dependencies if needed
cd "$SCRIPT_DIR"
if [[ ! -d "node_modules" ]]; then
    log_info "Installing dependencies..."
    npm install
fi

# Install Playwright browsers if needed
if [[ ! -d "$HOME/.cache/ms-playwright" ]] && [[ ! -d "/ms-playwright" ]]; then
    log_info "Installing Playwright browsers..."
    npx playwright install chromium
fi

# Build command
CMD="npx playwright test --project stripe-checkout"

if [[ -n "$HEADED" ]]; then
    CMD="$CMD $HEADED"
fi

if [[ -n "$DEBUG" ]]; then
    CMD="$CMD $DEBUG"
fi

# Run tests
log_info "Running Stripe E2E tests..."
log_info "Command: $CMD"
echo ""

$CMD

EXIT_CODE=$?

if [[ $EXIT_CODE -eq 0 ]]; then
    log_success "All tests passed!"
else
    log_error "Tests failed with exit code $EXIT_CODE"
    log_info "Check playwright-report/index.html for details"
fi

exit $EXIT_CODE
