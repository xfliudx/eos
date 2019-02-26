#!/usr/bin/env python3

from testUtils import Utils
from Cluster import Cluster
from WalletMgr import WalletMgr
from Node import Node
from Node import ReturnType
from TestHelper import TestHelper
from testUtils import Account

import urllib.request
import re
import os
import time
import signal
import subprocess
import shutil


Print = Utils.Print
errorExit = Utils.errorExit
cmdError = Utils.cmdError
relaunchTimeout = 5

# Parse command line arguments
args = TestHelper.parse_args({"-v"})
Utils.Debug = args.v

# Setup cluster and it's wallet manager
walletMgr=WalletMgr(True)
cluster=Cluster(walletd=True)
cluster.setWalletMgr(walletMgr)

def removeReversibleBlocks(nodeId):
   dataDir = Cluster.getDataDir(nodeId)
   reversibleBlocks = os.path.join(dataDir, "blocks", "reversible")
   shutil.rmtree(reversibleBlocks, ignore_errors=True)

def getHeadAndLib(node: Node):
   info = node.getInfo()
   head = int(info["head_block_num"])
   lib = int(info["last_irreversible_block_num"])
   forkDbHead =  int(info["fork_db_head_block_num"])
   return head, lib, forkDbHead

# List to contain the test result message
testResultMsgs = []
try:
   TestHelper.printSystemInfo("BEGIN")
   cluster.killall(allInstances=True)
   cluster.cleanup()
   numOfProducers = 4
   totalNodes = 9
   cluster.launch(
      prodCount=numOfProducers,
      totalProducers=numOfProducers,
      totalNodes=totalNodes,
      pnodes=1,
      useBiosBootFile=False,
      topo="mesh",
      specificExtraNodeosArgs={
         0:"--enable-stale-production",
         4:"--read-mode irreversible",
         6:"--read-mode irreversible",
         8:"--read-mode irreversible"})

   # Give some time for it to produce, so lib is advancing
   producingNodeId = 0
   producingNode = cluster.getNode(producingNodeId)
   time.sleep(60)

   # Kill all nodes, so we can test all node in isolated environment
   for clusterNode in cluster.nodes:
      clusterNode.kill(signal.SIGTERM)
   cluster.biosNode.kill(signal.SIGTERM)

   def stopBlockProduction():
      if not producingNode.killed:
         producingNode.kill(signal.SIGTERM)

   def resumeBlockProduction():
      if producingNode.killed:
         producingNode.relaunch(producingNodeId, "", timeout=relaunchTimeout)

   # Wrapper function to execute test
   # This wrapper function will resurrect the node to be tested, and shut it down by the end of the test
   def executeTest(nodeIdOfNodeToTest, runTestScenario):
      try:
         nodeToTest = cluster.getNode(nodeIdOfNodeToTest)
         # Resurrect killed node to be tested
         isRelaunchSuccess = nodeToTest.relaunch(nodeIdOfNodeToTest, "", timeout=relaunchTimeout)
         assert isRelaunchSuccess, "Fail to relaunch before running test scenario"
         runTestScenario(nodeIdOfNodeToTest, nodeToTest)
         # Kill node after use
         if not nodeToTest.killed: nodeToTest.kill(signal.SIGTERM)
         testResultMsgs.append("!!!TEST CASE #{} IS SUCCESSFUL".format(nodeIdOfNodeToTest))
      except Exception as e:
         testResultMsgs.append("!!!BUG IS CONFIRMED ON TEST CASE #{} {}".format(nodeIdOfNodeToTest, e))

   # 1st test case: Replay in irreversible mode with reversible blocks
   # Expectation: Node replays and launches successfully
   # Current Bug: duplicate block added error
   def replayInIrrModeWithRevBlocks(nodeIdOfNodeToTest, nodeToTest):
      # Kill node and replay in irreversible mode
      nodeToTest.kill(signal.SIGTERM)
      isRelaunchSuccess = nodeToTest.relaunch(nodeIdOfNodeToTest, "--read-mode irreversible --replay", timeout=relaunchTimeout)
      assert isRelaunchSuccess, "Fail to relaunch"

   # 2nd test case: Replay in irreversible mode without reversible blocks
   # Expectation: Node replays and launches successfully with lib == head == libBeforeShutdown
   # Current Bug: last_irreversible_block != the real lib (e.g. if lib is 1000, it replays up to 1000 saying head is 1000 and lib is 999)
   def replayInIrrModeWithoutRevBlocksAndCheckState(nodeIdOfNodeToTest, nodeToTest):
      # Track head block num and lib before shutdown
      headBeforeShutdown, libBeforeShutdown, _ = getHeadAndLib(nodeToTest)
      # Shut node, remove reversible blocks and relaunch
      nodeToTest.kill(signal.SIGTERM)
      removeReversibleBlocks(nodeIdOfNodeToTest)
      isRelaunchSuccess = nodeToTest.relaunch(nodeIdOfNodeToTest, "--replay --read-mode irreversible", timeout=relaunchTimeout)
      assert isRelaunchSuccess, "Fail to relaunch"
      # Check head block num and lib
      head, lib, _ = getHeadAndLib(nodeToTest)
      assert_msg = "Expecting head == libBeforeShutdown == lib == libBeforeShutdown however got " + \
                   "headBeforeShutdown {}, head {}, lib {}, libBeforeShutdown {}".format(
                   headBeforeShutdown, head, lib, libBeforeShutdown)
      assert (head == libBeforeShutdown and lib == libBeforeShutdown), assert_msg

   # 3rd test case: Switch mode speculative -> irreversible without replay and production disabled
   # Expectation: Node switches mode successfully
   def switchSpecToIrrMode(nodeIdOfNodeToTest, nodeToTest):
      # Relaunch in irreversible mode
      nodeToTest.kill(signal.SIGTERM)
      isRelaunchSuccess = nodeToTest.relaunch(nodeIdOfNodeToTest, "--read-mode irreversible", timeout=relaunchTimeout)
      assert isRelaunchSuccess, "Fail to relaunch"

   # 4th test case: Switch mode irreversible -> speculative without replay and production disabled
   # Expectation: Node switches mode successfully
   def switchIrrToSpecMode(nodeIdOfNodeToTest, nodeToTest):
      # Relaunch in speculative mode
      nodeToTest.kill(signal.SIGTERM)
      isRelaunchSuccess = nodeToTest.relaunch(nodeIdOfNodeToTest, "", timeout=relaunchTimeout, addOrSwapFlags={"--read-mode": "speculative"})
      assert isRelaunchSuccess, "Fail to relaunch"

   # 5th test case: Switch mode irreversible -> speculative without replay and production enabled
   # Expectation: Node switches mode successfully and receives next block from the producer
   # Current Bug: Fail to switch to irreversible mode, block_validate_exception next block in the future will be thrown
   def switchSpecToIrrModeWithProdEnabled(nodeIdOfNodeToTest, nodeToTest):
      try:
         # Resume block production
         resumeBlockProduction()
         # Kill and relaunch in irreversible mode
         nodeToTest.kill(signal.SIGTERM)
         time.sleep(60) # Wait for some blocks to be produced and lib advance
         isRelaunchSuccess = nodeToTest.relaunch(nodeIdOfNodeToTest, "--read-mode irreversible", timeout=relaunchTimeout)
         assert isRelaunchSuccess, "Fail to relaunch"
         # Ensure that the relaunched node received blocks from producers
         head, _, _ = getHeadAndLib(nodeToTest)
         time.sleep(60) # Wait until lib advance
         headAfterWaiting, _, _ = getHeadAndLib(nodeToTest)
         assert headAfterWaiting > head, "Head is not advancing"
      finally:
         # Stop block production
         stopBlockProduction()

   # 6th test case: Switch mode irreversible -> speculative without replay and production enabled
   # Expectation: Node switches mode successfully and receives next block from the producer
   # Current Bug: Node switches mode successfully, however, it fails to establish connection with the producing node
   def switchIrrToSpecModeWithProdEnabled(nodeIdOfNodeToTest, nodeToTest):
      try:
         # Resume block production
         resumeBlockProduction()
         # Kill and relaunch in irreversible mode
         nodeToTest.kill(signal.SIGTERM)
         time.sleep(60) # Wait for some blocks to be produced and lib advance
         isRelaunchSuccess = nodeToTest.relaunch(nodeIdOfNodeToTest, "", timeout=relaunchTimeout, addOrSwapFlags={"--read-mode": "speculative"})
         assert isRelaunchSuccess, "Fail to relaunch"
         # Ensure that the relaunched node received blocks from producers
         head, _, _ = getHeadAndLib(nodeToTest)
         time.sleep(5)
         headAfterWaiting, _, _ = getHeadAndLib(nodeToTest)
         assert headAfterWaiting > head, "Head is not advancing"
      finally:
         # Stop block production
         stopBlockProduction()

   # 7th test case: Switch mode speculative -> irreversible and check the state
   # Expectation: Node switch mode successfully and lib == libBeforeShutdown == head and forkDbBlockNum == forkDbBlockNumBeforeShutdown
   def switchSpecToIrrModeAndCheckState(nodeIdOfNodeToTest, nodeToTest):
      # Track head block num and lib before shutdown
      headBeforeShutdown, libBeforeShutdown, forkDbBlockNumBeforeShutdown = getHeadAndLib(nodeToTest)
      # Kill and relaunch in irreversible mode
      nodeToTest.kill(signal.SIGTERM)
      isRelaunchSuccess = nodeToTest.relaunch(nodeIdOfNodeToTest, "--read-mode irreversible", timeout=relaunchTimeout)
      assert isRelaunchSuccess, "Fail to relaunch"
      # Check head block num and lib
      head, lib, forkDbBlockNum = getHeadAndLib(nodeToTest)
      assert_msg = "Expecting head == libBeforeShutdown == lib == libBeforeShutdown and forkDbBlockNum == forkDbBlockNumBeforeShutdown however got " + \
                   "headBeforeShutdown {}, head {}, lib {}, libBeforeShutdown {}, forkDbBlockNum {}, forkDbBlockNumBeforeShutdown {}".format(
                   headBeforeShutdown, head, lib, libBeforeShutdown, forkDbBlockNum, forkDbBlockNumBeforeShutdown)
      assert (head == libBeforeShutdown and lib == libBeforeShutdown and forkDbBlockNum == forkDbBlockNumBeforeShutdown), assert_msg

   # 8th test case: Switch mode irreversible -> speculative and check the state
   # Expectation: Node switch mode successfully and lib == libBeforeShutdown and head == and forkDbBlockNum and forkDbBlockNum == forkDbBlockNumBeforeShutdown
   def switchIrrToSpecModeAndCheckState(nodeIdOfNodeToTest, nodeToTest):
      # Track head block num and lib before shutdown
      headBeforeShutdown, libBeforeShutdown, forkDbBlockNumBeforeShutdown = getHeadAndLib(nodeToTest)
      # Kill and relaunch in speculative mode
      nodeToTest.kill(signal.SIGTERM)
      isRelaunchSuccess = nodeToTest.relaunch(nodeIdOfNodeToTest, "", timeout=relaunchTimeout, addOrSwapFlags={"--read-mode": "speculative"})
      assert isRelaunchSuccess, "Fail to relaunch"

      # Check head block num and lib
      head, lib, forkDbBlockNum = getHeadAndLib(nodeToTest)
      assert_msg = "Expecting head == forkDbBlockNum and lib == libBeforeShutdown and forkDbBlockNum == forkDbBlockNumBeforeShutdown however got " + \
                   "headBeforeShutdown {}, head {}, lib {}, libBeforeShutdown {}, forkDbBlockNum {}, forkDbBlockNumBeforeShutdown {}".format(
                   headBeforeShutdown, head, lib, libBeforeShutdown, forkDbBlockNum, forkDbBlockNumBeforeShutdown)
      assert (head == forkDbBlockNum and lib == libBeforeShutdown and forkDbBlockNum == forkDbBlockNumBeforeShutdown), assert_msg

   # Start executing test cases here
   executeTest(1, replayInIrrModeWithRevBlocks)
   executeTest(2, replayInIrrModeWithoutRevBlocksAndCheckState)
   executeTest(3, switchSpecToIrrMode)
   executeTest(4, switchIrrToSpecMode)
   executeTest(5, switchSpecToIrrModeWithProdEnabled)
   executeTest(6, switchIrrToSpecModeWithProdEnabled)
   executeTest(7, switchSpecToIrrModeAndCheckState)
   executeTest(8, switchIrrToSpecModeAndCheckState)

finally:
   TestHelper.shutdown(cluster, walletMgr)
   # Print test result
   for msg in testResultMsgs: Print(msg)

exit(0)
