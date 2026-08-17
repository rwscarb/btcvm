# btcvm bash tab completion
# Install: cp completions/btcvm.bash ~/.local/share/bash-completion/completions/btcvm
#          source ~/.local/share/bash-completion/completions/btcvm

_btcvm_complete() {
    local cur prev words cword
    _init_completion || return

    local main_commands="--once --vdf-ticks --trace --vms --help"

    case "$prev" in
        --vdf-ticks)
            COMPREPLY=( $(compgen -W "5 10 20 50 100" -- "$cur") )
            return ;;
        --vms)
            COMPREPLY=( $(compgen -W "2 4 8 16" -- "$cur") )
            return ;;
    esac

    COMPREPLY=( $(compgen -W "$main_commands" -- "$cur") )
}

_imgfs_complete() {
    local cur prev words cword
    _init_completion || return

    local subcommands="add status commit list verify verify-chunk"

    if [ "$cword" -eq 1 ]; then
        COMPREPLY=( $(compgen -W "$subcommands" -- "$cur") )
        return
    fi

    local subcmd="${words[1]}"
    case "$subcmd" in
        add)
            # Complete any file
            COMPREPLY=( $(compgen -f -- "$cur") )
            ;;
        verify)
            # Complete files in manifest if available
            if [ -f imgfs_manifest.jsonl ]; then
                local names
                names=$(python3 -c "
import json
try:
    with open('imgfs_manifest.jsonl') as f:
        for line in f:
            e = json.loads(line.strip())
            print(e.get('name',''))
except: pass
" 2>/dev/null)
                COMPREPLY=( $(compgen -W "$names" -- "$cur") )
            else
                COMPREPLY=( $(compgen -f -- "$cur") )
            fi
            ;;
        verify-chunk)
            if [ "$cword" -eq 2 ]; then
                # Complete video filenames from manifest
                if [ -f imgfs_manifest.jsonl ]; then
                    local names
                    names=$(python3 -c "
import json
try:
    with open('imgfs_manifest.jsonl') as f:
        for line in f:
            e = json.loads(line.strip())
            if e.get('type') == 'video':
                print(e.get('name',''))
except: pass
" 2>/dev/null)
                    COMPREPLY=( $(compgen -W "$names" -- "$cur") )
                else
                    COMPREPLY=( $(compgen -f -- "$cur") )
                fi
            elif [ "$cword" -eq 3 ]; then
                # Complete chunk indices for the given video
                local vfile="${words[2]}"
                local n_chunks
                n_chunks=$(python3 -c "
import json
try:
    with open('imgfs_manifest.jsonl') as f:
        for line in f:
            e = json.loads(line.strip())
            if e.get('name') == '$vfile':
                print(e.get('n_chunks', 1))
except: pass
" 2>/dev/null)
                if [ -n "$n_chunks" ]; then
                    local indices
                    indices=$(seq 0 $(( n_chunks - 1 )))
                    COMPREPLY=( $(compgen -W "$indices" -- "$cur") )
                fi
            fi
            ;;
    esac
}

complete -F _btcvm_complete btcvm
complete -F _imgfs_complete imgfs.py
complete -F _imgfs_complete imgfs
