'use strict';
// Reuse electron-updater's shipped SemVer dependency, including prerelease ordering.
const semver=require(require.resolve('semver',{paths:[require.resolve('electron-updater')]}));
function installState(local,remote){
    if(!local)return 'install';
    if(local.marketplace?.source!==remote.source||local.marketplace?.owner_github_id!==remote.owner_github_id)return 'conflict';
    const current=semver.valid(String(local.version||'').trim()),latest=semver.valid(String(remote.version||'').trim());
    if(!current||!latest)return 'unknown';
    const order=semver.compare(current,latest);
    return order===0?'installed':order>0?'newer':'update';
}
module.exports={installState};
