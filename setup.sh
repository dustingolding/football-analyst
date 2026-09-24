# Create the virtual environment if needed.
if [ ! -d ".venv" ]; then
    py -m venv .venv || return 1
fi

# Activate it in the current shell.
activate || return 1

# Install project dependencies.
install

clear