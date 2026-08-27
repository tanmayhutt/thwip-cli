#!/usr/bin/env bash
# Install script for thwip (Universal Coding Agent Multiplexer)
set -e

echo "Installing thwip..."

if command -v uv &> /dev/null; then
    echo "Installing via uv tool..."
    uv tool install --force thwip-cli
elif command -v pipx &> /dev/null; then
    echo "Installing via pipx..."
    pipx install --force thwip-cli
elif command -v pip3 &> /dev/null; then
    echo "Installing via pip3..."
    pip3 install --upgrade thwip-cli
else
    echo "Error: Neither uv, pipx, nor pip3 was found on your system."
    echo "Please install Python (>= 3.11) or uv to use thwip."
    exit 1
fi

echo "thwip installed successfully. Run 'thwip' to get started."
