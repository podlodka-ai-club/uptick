// Run from cmd/reportfacts in a temporary copy of HackerSprint2_sim.
// Uses the existing read-only journal scanner; emits no credentials or headers.
package main

import (
 "context"
 "encoding/json"
 "flag"
 "fmt"
 "os"
 "strings"
 "github.com/aigizk/hackersprint2-sim/internal/persistence/journal"
 "github.com/aigizk/hackersprint2-sim/internal/simulation/events"
 "github.com/aigizk/hackersprint2-sim/internal/simulation/model"
)

type Fact struct { Version uint64 `json:"version"`; Type string `json:"type"`; Data json.RawMessage `json:"data"` }
type Block struct { Rule string `json:"rule"`; Revision uint64 `json:"revision"`; UserAgent string `json:"user_agent"`; Attack uint64 `json:"attack_requests"`; Other uint64 `json:"non_attack_requests"` }
type Run struct { ID string `json:"run_id"`; Facts []Fact `json:"facts"`; Commands map[string]int `json:"commands"`; Reads map[string]int `json:"http_requests"`; Blocks map[string]*Block `json:"firewall_blocks"`; Events uint64 `json:"event_count"`; Requests uint64 `json:"request_count"` }

func main() {
 root:=flag.String("data","","offline data root"); ids:=flag.String("ids","","comma-separated run ids");flag.Parse()
 selected:=map[string]bool{}
 for _,x:=range strings.Fields("WorldCreated RunEnded TrafficAttackStarted TrafficAttackEnded FirewallRuleUpserted FirewallRuleDeleted FirewallAvailabilityChanged BackendAvailabilityChanged DatabaseAvailabilityChanged ServerProvisioningStarted ServerActivated ServerDrainingStarted ServerRemoved DatabaseCreated DatabaseDeleted DatabaseBackupStarted DatabaseBackupCompleted DatabaseRestoreStarted DatabaseRestoreCompleted SiteDatabaseChanged SiteStopStarted SiteStopped SiteStarted DiskLogsCleaned ControlCommandAccepted") { selected[x]=true }
 output:=[]Run{}
 for _,id:=range strings.Split(*ids,",") {
  r:=Run{ID:id,Commands:map[string]int{},Reads:map[string]int{},Blocks:map[string]*Block{}}
  requestAttacks:=map[model.RequestID]model.AttackID{}
  err:=journal.ScanReadOnly(context.Background(),*root,id,func(x journal.ReadOnlyRecord)error{
   if x.RequestReceived!=nil { r.Requests++;p:=strings.ReplaceAll(x.RequestReceived.Path,id,"{run_id}");r.Reads[x.RequestReceived.Method+" "+p]++ }
   if x.EventType=="" {return nil};r.Events++
   if selected[x.EventType] { r.Facts=append(r.Facts,Fact{x.DomainVersion,x.EventType,append(json.RawMessage(nil),x.EventPayload...)}) }
   switch x.EventType {
   case "ControlCommandAccepted": var e events.ControlCommandAccepted;if err:=json.Unmarshal(x.EventPayload,&e);err!=nil{return err};r.Commands[e.Command]++
   case "PageRequestStarted":var e events.PageRequestStarted;if err:=json.Unmarshal(x.EventPayload,&e);err!=nil{return err};requestAttacks[e.RequestID]=e.AttackID
   case "FirewallRequestEvaluated":var e events.FirewallRequestEvaluated;if err:=json.Unmarshal(x.EventPayload,&e);err!=nil{return err};attack:=requestAttacks[e.RequestID];delete(requestAttacks,e.RequestID)
    if e.Action==model.FirewallDeny {k:=fmt.Sprintf("%s:%d:%s",e.MatchedRuleID,e.MatchedRuleRevision,e.UserAgent);b:=r.Blocks[k];if b==nil {b=&Block{Rule:string(e.MatchedRuleID),Revision:e.MatchedRuleRevision,UserAgent:e.UserAgent};r.Blocks[k]=b};if attack==""{b.Other++}else{b.Attack++}}
   }
   return nil
  });if err!=nil{fmt.Fprintln(os.Stderr,err);os.Exit(1)};output=append(output,r)
 }
 enc:=json.NewEncoder(os.Stdout);enc.SetIndent("","  ");if err:=enc.Encode(output);err!=nil{panic(err)}
}
