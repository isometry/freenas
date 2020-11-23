#
# SMB.CONF(5)		The configuration file for the Samba suite 
# $FreeBSD$
#
<%
    smb_ha_mode = middleware.call_sync('smb.get_smb_ha_mode')
    failover_status = middleware.call_sync('failover.status')
%>

[global]
    % if failover_status in ('SINGLE', 'MASTER'):
    % if smb_ha_mode == "CLUSTERED":
    clustering = Yes
    % endif
    config backend = registry
    % else:
    netbiosname = TN_STANDBY
    % endif
